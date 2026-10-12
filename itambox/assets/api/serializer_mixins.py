from collections.abc import Mapping

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from assets.specification_adapters import native_persistence_fields


class CanonicalSpecificationSerializerMixin(serializers.Serializer):
    # Keep API parsing separate from the canonical command writer.

    specification_patch = serializers.JSONField(write_only=True, required=False)

    def validate_specification_patch(self, value):
        if not isinstance(value, Mapping):
            raise serializers.ValidationError(_("Custom field specification patch must be an object."))
        unknown_operations = set(value) - {"set", "clear"}
        if unknown_operations:
            raise serializers.ValidationError(_("Custom field patch contains an unknown operation."))
        submitted = value.get("set", {})
        clear_keys = value.get("clear", [])
        if not isinstance(submitted, Mapping):
            raise serializers.ValidationError(_("Custom field patch 'set' must be an object."))
        if not isinstance(clear_keys, list) or any(not isinstance(key, str) for key in clear_keys):
            raise serializers.ValidationError(_("Custom field patch 'clear' must be a list of field keys."))
        return {"set": dict(submitted), "clear": list(clear_keys)}

    def _request_user(self):
        request = self.context.get("request") if hasattr(self, "context") else None
        return getattr(request, "user", None)

    @staticmethod
    def _persist_native_update(current, validated_data):
        # Commands acquire catalogue/owner locks first. Reload their
        # result before native-only persistence so stale JSON cannot overwrite it.
        # unscoped: post-command re-read of the locked owner must not depend on soft-delete state or ambient scope
        current = type(current)._base_manager.get(pk=current.pk)
        concrete_names = {field.name for field in current._meta.concrete_fields}
        native_fields = []
        for field_name, value in validated_data.items():
            if field_name in concrete_names:
                setattr(current, field_name, value)
                native_fields.append(field_name)
        if native_fields:
            current.save(update_fields=native_persistence_fields(current, native_fields))
        return current

    @staticmethod
    def _command_error(exc):
        if isinstance(exc, PermissionDenied):
            raise DRFPermissionDenied("You do not have permission to perform this action.") from exc
        if isinstance(exc, DjangoValidationError):
            message_dict = getattr(exc, "message_dict", None)
            if isinstance(message_dict, dict) and message_dict:
                detail = {str(field): "Invalid value." for field in message_dict}
            else:
                detail = "The submitted data is invalid."
            raise serializers.ValidationError(detail) from exc
        raise exc
