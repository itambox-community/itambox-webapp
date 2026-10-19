# itambox/core/forms/__init__.py
#
# Re-exports all framework-level form classes from core.forms.mixins.
# Domain forms that depend on extras (Webhook, EventRule, Label, Report,
# ScheduledReport, AlertRule, NotificationChannel, ObjectChangeFilter) have
# moved to extras/forms.py.  This module no longer imports extras at all,
# breaking the eager extras dependency.

from .base import TenantScopedFormMixin, apply_tom_select, tenant_rows_exist
from .choices import apply_empty_choice_first, empty_choice_first
from .mixins import (
    BULK_EDIT_FIELD_BLACKLIST,
    BULK_EDIT_FIELD_TYPE_MAP,
    OBJ_TYPE_CHOICES,
    BulkEditForm,
    ColorFieldFormMixin,
    ConfirmationForm,
    CrispyFormMixin,
    FilterForm,
    JournalEntryForm,
    SearchForm,
    SlugModelForm,
    cancel_button,
)
from .scoping import (
    TenantScopedAdminFormMixin,
    TenantScopedFilterSetMixin,
    TenantScopedModelChoiceField,
    TenantScopedModelMultipleChoiceField,
    apply_tenant_scoped_choices,
)
from .tenant import scope_tenant_field, scope_tenant_group_field

__all__ = [
    # mixins / base forms
    "OBJ_TYPE_CHOICES",
    "SearchForm",
    "JournalEntryForm",
    "ConfirmationForm",
    "BULK_EDIT_FIELD_BLACKLIST",
    "BULK_EDIT_FIELD_TYPE_MAP",
    "BulkEditForm",
    "CrispyFormMixin",
    "cancel_button",
    "SlugModelForm",
    "FilterForm",
    "ColorFieldFormMixin",
    "scope_tenant_field",
    "scope_tenant_group_field",
    "TenantScopedFormMixin",
    "TenantScopedAdminFormMixin",
    "TenantScopedFilterSetMixin",
    "TenantScopedModelChoiceField",
    "TenantScopedModelMultipleChoiceField",
    "apply_tenant_scoped_choices",
    "apply_empty_choice_first",
    "empty_choice_first",
    "apply_tom_select",
    "tenant_rows_exist",
]
