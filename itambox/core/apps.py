from django.apps import AppConfig
from django.contrib.admin.apps import AdminConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _

from core.schedules import register_schedule


class SuperuserAdminConfig(AdminConfig):
    default_site = "core.admin.SuperuserAdminSite"


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = _("Core")

    def ready(self):
        # Register the all-or-nothing guarded vendor resubmission action.
        # with the all-or-nothing guarded variant. Importing the guard module
        # here runs no ORM queries.
        # inline import: app-registry: avoid AppRegistryNotReady at app-load time
        from django.contrib import admin
        from django_q.models import Failure, Success

        # inline import: app-registry: core.signals imports models and registers receivers, which needs the loaded app registry
        import core.signals  # noqa: F401 -- registers core signal receivers

        # inline import: app-registry: register the production configuration checks after app loading
        from core import checks  # noqa: F401 -- registers Django system checks

        # inline import: app-registry: avoid AppRegistryNotReady at app-load time
        from core.django_q_task_resubmission import GuardedFailAdmin, GuardedTaskAdmin

        admin.site.unregister(Success)
        admin.site.unregister(Failure)
        admin.site.register(Success, GuardedTaskAdmin)
        admin.site.register(Failure, GuardedFailAdmin)

        post_migrate.connect(self._register_prune_schedule, sender=self)

    def _register_prune_schedule(self, sender, **kwargs):
        """Ensure the daily changelog/operational-data retention prune schedule exists."""
        # inline import: app-registry: avoid AppRegistryNotReady at app-load time
        from django_q.models import Schedule

        register_schedule(
            "core.tasks.prune_changelog_task",
            defaults={
                "name": "Daily Changelog & Operational-Data Retention Prune",
                "schedule_type": Schedule.DAILY,
                "repeats": -1,
            },
        )
