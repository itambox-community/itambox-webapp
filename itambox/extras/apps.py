from django.apps import AppConfig
from django.core.exceptions import FieldDoesNotExist
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.recorder import MigrationRecorder
from django.db.models.signals import post_migrate

from core.schedules import register_schedule
from itambox.capabilities import (
    ALWAYS_ON,
    CAPABILITY_REGISTRY_DOC_URL,
    CONTRACT_VERSION,
    SOURCE_ALWAYS,
    STABLE,
    Capability,
)
from itambox.capabilities import (
    registry as capability_registry,
)
from itambox.registry import registry as generic_presentation_registry

DOCS = CAPABILITY_REGISTRY_DOC_URL

# Canonical schedules are registered only once the extras schema they depend on
# is applied on the database being migrated, so a half-migrated database never
# gains task paths that its code cannot execute yet.
SCHEDULE_PREREQUISITE = ("extras", "0002_cross_app_relations")
ALERT_DISPATCH_UID = "extras.issue445.register_daily_alert_schedule.v1"
ALERT_TASK_PATH = "extras.tasks.alerts.evaluate_alert_rules_task"
WEBHOOK_RECOVERY_TASK_PATH = "extras.tasks.webhooks.recover_pending_webhook_deliveries"


class ExtrasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "extras"

    def ready(self):
        # inline imports: app-registry: extras.signals, extras.search and extras.feature_views
        # register after app population
        import extras.search
        import extras.signals  # noqa: F401 -- side-effect import connects the event/watcher receivers
        from extras.feature_views import EXTRAS_GENERIC_PRESENTATION_PROVIDER

        self._register_capabilities()
        self._register_generic_presentation(EXTRAS_GENERIC_PRESENTATION_PROVIDER)
        self._register_custom_field_data_validators()
        # ready() must stay query-free: the applied-migration probe below belongs
        # to the post_migrate handler, never to app loading.
        post_migrate.connect(
            self._register_alert_schedule,
            sender=self,
            dispatch_uid=ALERT_DISPATCH_UID,
            weak=False,
        )

    def _register_custom_field_data_validators(self):
        # inline import: app-registry: defer custom-field helpers until all models are loaded
        from extras.customfields import validate_custom_field_data_owner

        for model in self.apps.get_models():
            try:
                model._meta.get_field("custom_field_data")
            except FieldDoesNotExist:
                continue
            generic_presentation_registry.register_custom_field_data_validator(
                model,
                validate_custom_field_data_owner,
            )

    def _register_alert_schedule(self, sender, using=DEFAULT_DB_ALIAS, **kwargs):
        """Ensure the alert-evaluation and webhook-recovery periodic tasks exist.

        They are registered after the extras schema is applied and are
        idempotent, so repeated migrate runs keep exactly one row each.
        """
        # inline import: app-registry: avoid AppRegistryNotReady at app-load time
        from django_q.models import Schedule

        connection = connections[using]
        recorder = MigrationRecorder(connection)
        if not recorder.has_table():
            return
        app, name = SCHEDULE_PREREQUISITE
        if not recorder.migration_qs.filter(app=app, name=name).exists():
            return
        register_schedule(
            ALERT_TASK_PATH,
            defaults={
                "name": "Daily Alert Rule Evaluation",
                "schedule_type": Schedule.DAILY,
                "repeats": -1,
            },
            using=using,
        )
        register_schedule(
            WEBHOOK_RECOVERY_TASK_PATH,
            defaults={
                "name": "Webhook Delivery Recovery",
                "schedule_type": Schedule.MINUTES,
                "minutes": 1,
                "repeats": -1,
            },
            using=using,
        )

    def _register_capabilities(self):
        """Declare the reporting, alerting, and automation slices.

        ``extras`` is the clearest case for a capability registry: one Django
        app holds the curated report catalogue, the report designer, scheduled
        reports, the alert inbox, alert rules, and webhook automation. Each is
        declared as its own slice with its own published grade, so a single
        app-level grade could only ever be wrong in one direction or the other.

        ``register_all`` rather than a loop: ``ready()`` runs again whenever a
        test swaps ``INSTALLED_APPS``, and with six entries a failure partway
        through has to be finishable on the next run.
        """
        capability_registry.register_all(self._capabilities())

    def _register_generic_presentation(self, provider):
        job_model = self.apps.get_model("core", "Job")
        generic_presentation_registry.register_feature(job_model, "job_file_attachments")
        generic_presentation_registry.register_generic_presentation(
            "extras",
            provider,
            detail_features=(
                "bookmarkable",
                "custom_field_data",
                "file_attachments",
                "image_attachments",
                "job_file_attachments",
                "journaling",
                "watchable",
            ),
            list_params=True,
            list_filter=True,
            list_context=True,
            priority=100,
        )

    def _capabilities(self):
        return (
            Capability(
                key="reporting.curated",
                title="Curated Reports",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("core.reports",),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="reporting.designer",
                title="Report Designer",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("extras.ReportTemplate",),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="reporting.scheduled",
                title="Scheduled Reports",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("extras.ReportGenerationArchive", "extras.ScheduledReport"),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="alerting.inbox",
                title="Alerts and Notifications",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("core.Notification", "extras.AlertLog"),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="alerting.rules",
                title="Alert Rules and Channels",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("extras.AlertRule", "extras.NotificationChannel"),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
            Capability(
                key="automation.webhooks",
                title="Webhooks and Event Rules",
                owning_area="area:operations",
                maturity=STABLE,
                security_critical=False,
                activation=ALWAYS_ON,
                activation_probe=None,
                activation_source=SOURCE_ALWAYS,
                owns=("extras.EventRule", "extras.WebhookEndpoint"),
                docs_url=DOCS,
                limitations=(),
                contract_version=CONTRACT_VERSION,
            ),
        )
