from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from assets.models import AssetMaintenance


class StatusModelChoiceField(forms.ModelChoiceField):
    def to_python(self, value):
        if value in self.empty_values:
            return None
        if isinstance(value, str) and not value.isdigit():
            try:
                return self.queryset.get(Q(slug=value) | Q(name__iexact=value))
            except self.queryset.model.DoesNotExist:
                raise ValidationError(self.error_messages["invalid_choice"], code="invalid_choice") from None
        return super().to_python(value)


def selectable_repair_maintenances(current_id=None):
    """Repair-maintenance choices for the lifecycle record forms (#644).

    Lists the tenant's repair maintenances and keeps the maintenance a record
    already links to selectable even when it sits in the recycle bin: the link on
    the record must never be silently dropped by an edit that only changes another
    field. The already-linked row is the only one read unscoped - it has to be
    resolvable although the recycle bin hides it.
    """
    queryset = AssetMaintenance.objects.filter(maintenance_type=AssetMaintenance.MAINTENANCE_TYPE_REPAIR)
    if current_id:
        # unscoped: an already-linked maintenance stays selectable even soft-deleted
        queryset = queryset | AssetMaintenance.all_objects.filter(pk=current_id)
    return queryset
