from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.managers import get_current_tenant
from core.validators import validate_external_url
from extras.definition_contract import custom_field_definition_contract_errors
from extras.models import (
    AlertLog,
    AlertRule,
    CustomField,
    CustomFieldChoiceSet,
    CustomFieldset,
    Dashboard,
    Event,
    EventRule,
    JournalEntry,
    NotificationChannel,
    Tag,
    WebhookDelivery,
    WebhookEndpoint,
    alert_rule_channel_scope_errors,
)
from itambox.api.base import BaseModelSerializer
from itambox.api.fields import ContentTypeField, validate_gfk_target_tenant
from itambox.registry import registry

# Keys in NotificationChannel.config that hold credentials (Slack/Teams incoming-
# webhook URLs, bearer tokens, etc.). These are redacted on READ so an API reader
# cannot exfiltrate them, and preserved on WRITE so a read-modify-write round-trip
# does not persist the mask. Mirrors WebhookEndpoint.secret being write-only.
_SECRET_CONFIG_HINTS = ("webhook_url", "secret", "password", "token", "api_key", "apikey", "auth")
_REDACTED_PLACEHOLDER = "•" * 8  # eight bullets
_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE = _(
    "Event rule conditions are withdrawn for the 1.0 release. Existing conditions are preserved and remain readable; "
    "new or changed conditions cannot be submitted."
)


def _is_secret_config_key(key):
    k = str(key).lower()
    return any(hint in k for hint in _SECRET_CONFIG_HINTS)


class TagSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:tag-detail")

    class Meta:
        model = Tag
        fields = ["id", "url", "name", "slug", "color", "description", "created_at", "updated_at"]
        brief_fields = ["id", "url", "name", "slug", "color"]


class CustomFieldSerializer(BaseModelSerializer):
    field_type_display = serializers.CharField(source="get_field_type_display", read_only=True)

    choice_set = serializers.PrimaryKeyRelatedField(
        queryset=CustomFieldChoiceSet.objects.filter(lifecycle=CustomFieldChoiceSet.LIFECYCLE_ACTIVE),
        required=False,
        allow_null=True,
    )
    object_types = serializers.SlugRelatedField(
        many=True,
        queryset=ContentType.objects.all(),
        slug_field="model",
        required=False,
    )

    class Meta:
        model = CustomField
        fields = [
            "id",
            "name",
            "namespace",
            "label",
            "help_text",
            "field_type",
            "field_type_display",
            "activation",
            "quantity_kind",
            "canonical_unit",
            "minimum_value",
            "maximum_value",
            "regex",
            "decimal_scale",
            "max_values",
            "text_max_length",
            "validation_rule",
            "required",
            "nullable",
            "mappings",
            "choice_set",
            "object_types",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "name", "label", "field_type"]

    def validate(self, data):
        instance = self.instance
        object_types = list(data["object_types"] or []) if "object_types" in data else []
        if instance is not None and "object_types" not in data:
            object_types = list(instance.object_types.all())
        supported_object_types = {
            (model._meta.app_label, model._meta.model_name)
            for model, features in registry.model_features.items()
            if "custom_field_data" in features and not model._meta.abstract
        }
        unsupported_object_types = [
            content_type.model
            for content_type in object_types
            if (content_type.app_label, content_type.model) not in supported_object_types
        ]
        if unsupported_object_types:
            message = _("Unsupported custom-field owner: %(models)s.") % {
                "models": ", ".join(unsupported_object_types),
            }
            raise serializers.ValidationError({"object_types": [message]})
        errors = custom_field_definition_contract_errors(
            field_type=data.get("field_type", getattr(instance, "field_type", None)),
            activation=data.get("activation", getattr(instance, "activation", None)),
            quantity_kind=data.get("quantity_kind", getattr(instance, "quantity_kind", None)),
            canonical_unit=data.get("canonical_unit", getattr(instance, "canonical_unit", None)),
            minimum_value=data.get("minimum_value", getattr(instance, "minimum_value", None)),
            maximum_value=data.get("maximum_value", getattr(instance, "maximum_value", None)),
            regex=data.get("regex", getattr(instance, "regex", None)),
            decimal_scale=data.get("decimal_scale", getattr(instance, "decimal_scale", None)),
            max_values=data.get("max_values", getattr(instance, "max_values", None)),
            text_max_length=data.get("text_max_length", getattr(instance, "text_max_length", None)),
            validation_rule=data.get("validation_rule", getattr(instance, "validation_rule", None)),
            mappings=data.get("mappings", getattr(instance, "mappings", None)),
            choice_set=data.get("choice_set", getattr(instance, "choice_set", None)),
            object_types=object_types,
            management_kind=data.get("management_kind", getattr(instance, "management_kind", "local")),
            lifecycle=data.get("lifecycle", getattr(instance, "lifecycle", "active")),
            name=data.get("name", getattr(instance, "name", None)),
            namespace=data.get("namespace", getattr(instance, "namespace", None)),
        )
        if instance is not None:
            for model_field in CustomField.immutable_fields:
                api_field = "choice_set" if model_field == "choice_set_id" else model_field
                if api_field not in self.initial_data:
                    continue
                current = getattr(instance, model_field)
                if api_field == "choice_set":
                    current = instance.choice_set_id
                    incoming = getattr(data.get(api_field), "pk", data.get(api_field))
                else:
                    incoming = data.get(api_field)
                if incoming != current:
                    errors[api_field] = [_("This value is immutable after creation.")]
        if errors:
            raise serializers.ValidationError(errors)
        return data


class CustomFieldsetSerializer(BaseModelSerializer):
    fields = CustomFieldSerializer(many=True, read_only=True)

    def validate(self, data):
        if self.instance is not None:
            errors = {
                field: [_("This value is immutable after creation.")]
                for field in CustomFieldset.immutable_fields
                if field in data and data[field] != getattr(self.instance, field)
            }
            if errors:
                raise serializers.ValidationError(errors)
        return data

    class Meta:
        model = CustomFieldset
        fields = ["id", "namespace", "slug", "label", "description", "fields", "created_at", "updated_at"]
        brief_fields = ["id", "namespace", "slug", "label"]


class DashboardSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Dashboard
        fields = ["id", "user", "layout", "created", "last_updated"]
        brief_fields = ["id", "user"]

    def create(self, validated_data):
        # Dashboards are personal objects: the owner is always the
        # authenticated requester.  `user` is exposed read-only so a client
        # can never choose another owner, and without this the insert
        # crashed with IntegrityError -> HTTP 500. The
        # serializer is API-only (DashboardViewSet always provides a request
        # context).
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)

    def validate_layout(self, value):
        # The model contract is an ordered list of widget config dicts;
        # anything else would corrupt the dashboard UI and previously
        # slipped through as a silently accepted create.
        if not isinstance(value, list):
            raise serializers.ValidationError("Layout must be a list of widget configs.")
        return value


class WebhookEndpointSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:webhookendpoint-detail")
    # The model's own URLField is named `url`, which collides with the API
    # self-link `url` above; expose the target endpoint as `target_url`.
    target_url = serializers.URLField(source="url", max_length=2000)
    http_method_display = serializers.CharField(source="get_http_method_display", read_only=True)
    # `secret` is stored encrypted (model.save() encrypts; secret_decrypted reads).
    # Accept it write-only and let model.save() encrypt; the ciphertext/plaintext
    # is NEVER serialized out (mirrors License.product_key being omitted entirely).
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = WebhookEndpoint
        fields = [
            "id",
            "url",
            "name",
            "target_url",
            "http_method",
            "http_method_display",
            "headers",
            "secret",
            "enabled",
            "retry_count",
            "retry_backoff",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "url", "name", "enabled"]

    def validate_target_url(self, value: str) -> str:
        # SSRF guard at the API write boundary (BaseModelSerializer does not run full_clean,
        # so WebhookEndpoint.clean() would not fire otherwise). Reject internal targets at
        # create/update instead of only at dispatch time.

        try:
            validate_external_url(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value


class WebhookDeliverySerializer(BaseModelSerializer):
    """Read-only operational history for one webhook delivery.

    Delivery records deliberately expose relationship identifiers and display
    names only.  Endpoint URL, headers, and secrets are not fields on this
    serializer, so they cannot leak through either list or detail responses.
    """

    endpoint = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    endpoint_name = serializers.CharField(source="endpoint.name", read_only=True, allow_null=True)
    event = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    tenant = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    redelivered_from = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    redelivered_by = serializers.PrimaryKeyRelatedField(read_only=True, allow_null=True)
    redelivered_by_username = serializers.CharField(
        source="redelivered_by.username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = WebhookDelivery
        fields = [
            "id",
            "delivery_id",
            "status",
            "attempt",
            "response_code",
            "error_class",
            "error_message",
            "next_retry_at",
            "test_send",
            "redelivered_from",
            "redelivered_by",
            "redelivered_by_username",
            "redelivered_at",
            "attempted_at",
            "completed_at",
            "created_at",
            "updated_at",
            "endpoint",
            "endpoint_name",
            "event",
            "tenant",
        ]
        read_only_fields = fields
        brief_fields = ["id", "delivery_id", "status", "attempt", "test_send", "created_at"]


class WebhookDeliveryActionSerializer(serializers.Serializer):
    """Stable response body shared by redelivery and test-send actions."""

    id = serializers.IntegerField(read_only=True)
    delivery_id = serializers.CharField(read_only=True)


class EventRuleSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:eventrule-detail")
    model = ContentTypeField(queryset=ContentType.objects.all())
    action_type_display = serializers.CharField(source="get_action_type_display", read_only=True)
    webhook = WebhookEndpointSerializer(read_only=True)
    # WebhookEndpoint.objects is the tenant-scoped manager, so a rule can only
    # point at a same-tenant (or system-wide) webhook; this mirrors
    # EventRule.clean()'s same-tenant guard at the write boundary.
    webhook_id: serializers.PrimaryKeyRelatedField[WebhookEndpoint] = serializers.PrimaryKeyRelatedField(
        queryset=WebhookEndpoint.objects,
        source="webhook",
        write_only=True,
        required=False,
        allow_null=True,
    )

    def validate_events(self, value: list[str]) -> list[str]:
        """Reject unsupported event action values (WP-16a — closed vocabulary)."""
        valid = {choice[0] for choice in Event.ACTION_CHOICES}
        if not isinstance(value, list):
            raise serializers.ValidationError("events must be a list of action strings.")
        unknown = [v for v in value if v not in valid]
        if unknown:
            raise serializers.ValidationError(
                f"Unsupported event action(s): {', '.join(unknown)}. Accepted values: {', '.join(sorted(valid))}."
            )
        return value

    def validate_conditions(self, value: dict[str, object] | None) -> dict[str, object] | None:
        if self.instance is None:
            if value:
                raise serializers.ValidationError(_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE)
        elif value != self.instance.conditions:
            raise serializers.ValidationError(_EVENT_RULE_CONDITIONS_WITHDRAWN_MESSAGE)
        return value

    conditions_withdrawn = serializers.BooleanField(read_only=True)

    class Meta:
        model = EventRule
        fields = [
            "id",
            "url",
            "name",
            "model",
            "events",
            "conditions",
            "conditions_withdrawn",
            "action_type",
            "action_type_display",
            "webhook",
            "webhook_id",
            "action_config",
            "enabled",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "url", "name", "action_type", "enabled"]


class NotificationChannelSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:notificationchannel-detail")
    channel_type_display = serializers.CharField(source="get_channel_type_display", read_only=True)

    class Meta:
        model = NotificationChannel
        # `config` is a JSONField that can carry a credential (Slack/Teams
        # webhook_url). Secret-ish keys are redacted on read (to_representation) and
        # preserved on write (validate_config) so the URL is never exposed via the API.
        fields = [
            "id",
            "url",
            "name",
            "channel_type",
            "channel_type_display",
            "enabled",
            "config",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "url", "name", "channel_type", "enabled"]

    def to_representation(self, instance: object) -> dict[str, object]:
        data = super().to_representation(instance)
        cfg = data.get("config")
        if isinstance(cfg, dict):
            data["config"] = {
                k: (_REDACTED_PLACEHOLDER if (_is_secret_config_key(k) and v not in (None, "")) else v)
                for k, v in cfg.items()
            }
        return data

    def validate_config(self, value: object) -> object:
        # Restore redacted secrets from the stored config so a read-modify-write
        # round-trip (which echoes back the placeholder) does not overwrite the real
        # value; drop the placeholder entirely when there is nothing to restore.
        if isinstance(value, dict):
            existing = (self.instance.config or {}) if self.instance else {}
            cleaned = {}
            for k, v in value.items():
                if _is_secret_config_key(k) and v == _REDACTED_PLACEHOLDER:
                    if k in existing:
                        cleaned[k] = existing[k]
                    # else: nothing to restore -> drop the placeholder
                else:
                    cleaned[k] = v
            # SSRF guard: a Slack/Teams channel's webhook_url is an outbound target — reject
            # internal URLs at write time (a newly-supplied value, not a restored placeholder).
            url_val = cleaned.get("webhook_url")
            if url_val and url_val != _REDACTED_PLACEHOLDER:
                try:
                    validate_external_url(url_val)
                except DjangoValidationError as exc:
                    raise serializers.ValidationError({"webhook_url": exc.messages}) from exc
            return cleaned
        return value


class AlertRuleSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:alertrule-detail")
    alert_type_display = serializers.CharField(source="get_alert_type_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)
    channels = NotificationChannelSerializer(many=True, read_only=True)
    # NotificationChannel.objects is tenant-scoped, so a rule can only notify
    # via same-tenant channels.
    channel_ids = serializers.PrimaryKeyRelatedField(
        queryset=NotificationChannel.objects,
        source="channels",
        write_only=True,
        many=True,
        required=False,
    )

    class Meta:
        model = AlertRule
        fields = [
            "id",
            "url",
            "name",
            "description",
            "alert_type",
            "alert_type_display",
            "threshold_value",
            "severity",
            "severity_display",
            "is_active",
            "is_muted",
            "renotify_interval_days",
            "last_fired_at",
            "channels",
            "channel_ids",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "url", "name", "alert_type", "severity", "is_active"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Scope guard: a rule notifies only through channels of
        # its own scope. Fail the write instead of accepting a configuration
        # that can never deliver. ``tenant`` is not a writable API field, so
        # the target scope is the stored tenant on updates and the create-time
        # resolution of ``ITAMBoxModelViewSet._tenant_create_kwargs`` on
        # creates: a superuser create without a tenant field yields a
        # platform-wide rule, every other create is bound to the active
        # tenant scope.
        channels = attrs.get("channels")
        if channels:
            if self.instance is not None:
                tenant = self.instance.tenant
            else:
                context = getattr(self, "_context", None) or {}
                user = getattr(context.get("request"), "user", None)
                tenant = None if getattr(user, "is_superuser", False) else get_current_tenant()
            errors = alert_rule_channel_scope_errors(channels, tenant.pk if tenant else None)
            if errors:
                raise serializers.ValidationError({"channel_ids": errors})
        return attrs


class AlertLogSerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:alertlog-detail")
    rule_display = serializers.StringRelatedField(source="rule", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    severity_display = serializers.CharField(source="get_severity_display", read_only=True)
    acknowledged_by_display = serializers.StringRelatedField(source="acknowledged_by", read_only=True)
    resolved_by_display = serializers.StringRelatedField(source="resolved_by", read_only=True)
    content_object_display = serializers.SerializerMethodField()

    class Meta:
        model = AlertLog
        fields = [
            "id",
            "url",
            "rule",
            "tenant",
            "rule_display",
            "subject",
            "message",
            "severity",
            "severity_display",
            "content_type",
            "object_id",
            "content_object_display",
            "status",
            "status_display",
            "delivery_status",
            "delivery_outcome",
            "delivery_attempts",
            "last_delivery_id",
            "last_delivery_error",
            "last_notified_at",
            "acknowledged_by",
            "acknowledged_by_display",
            "resolved_by",
            "resolved_by_display",
            "resolution_notes",
            "resolved_at",
            "tenant_resolution_status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
        brief_fields = ["id", "url", "subject", "severity", "status", "created_at"]

    @extend_schema_field(OpenApiTypes.STR)
    def get_content_object_display(self, instance):
        obj = instance.content_object_safe
        return str(obj) if obj is not None else None


class JournalEntrySerializer(BaseModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name="api:extras_api:journalentry-detail")
    model = ContentTypeField(queryset=ContentType.objects.all())
    # Author is read-only: stamped from the request on create (see validate) and
    # immutable thereafter — journal entries are an audit trail.
    user_display: serializers.StringRelatedField[models.Model] = serializers.StringRelatedField(
        source="user", read_only=True
    )

    class Meta:
        model = JournalEntry
        fields = [
            "id",
            "url",
            "model",
            "object_id",
            "user_display",
            "comment",
            "created",
            "created_at",
            "updated_at",
        ]
        brief_fields = ["id", "url", "model", "object_id"]

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        if self.instance is None:
            # Create: stamp the author and verify the journaled object is visible
            # within the active tenant. validate_gfk_target_tenant resolves via
            # the target's default manager AND compares obj.tenant to the active
            # tenant, so it also guards tenant-owned models whose default manager
            # is NOT tenant-scoping (Dashboard, Job, Token, Membership) — a
            # plain .exists() check would let those through cross-tenant.
            request = self.context.get("request")
            if request is not None and request.user.is_authenticated:
                attrs["user"] = request.user
            validate_gfk_target_tenant(attrs.get("model"), attrs.get("object_id"))
        else:
            # Update: the journaled object (model/object_id) and the author are
            # immutable — an entry stays attached to its original subject and
            # author. Drop any attempt to change them.
            attrs.pop("model", None)
            attrs.pop("object_id", None)
            attrs.pop("user", None)
        return attrs
