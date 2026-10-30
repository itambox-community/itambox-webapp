from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.utils.translation import gettext_lazy as _

from core.forms import TenantScopedAdminFormMixin
from users.models import GroupMembership

if admin.site.is_registered(Group):
    admin.site.unregister(Group)

# Membership now lives in organization.models — registered there.

User = get_user_model()


@admin.register(User)
class UserAdmin(TenantScopedAdminFormMixin, BaseUserAdmin):
    """Stock Django UserAdmin extended to surface the custom ``can_login`` flag."""

    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_active",
        "can_login",
        "is_staff",
        "is_superuser",
    )
    list_filter = BaseUserAdmin.list_filter + ("can_login",)
    fieldsets = BaseUserAdmin.fieldsets + (
        (
            _("Login capability"),
            {
                "fields": ("can_login",),
                "description": _(
                    "Whether this user may perform interactive login (password or SSO). "
                    "Independent of 'active' status and API-token access."
                ),
            },
        ),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + ((_("Login capability"), {"fields": ("can_login",)}),)


@admin.register(GroupMembership)
class GroupMembershipAdmin(TenantScopedAdminFormMixin, admin.ModelAdmin):
    list_display = ("user_group", "membership", "source", "external_id", "added_at")
    list_filter = ("source", "user_group__tenant")
    search_fields = (
        "user_group__name",
        "membership__user__username",
        "membership__tenant__name",
        "external_id",
    )
    raw_id_fields = ("user_group", "membership", "added_by")
