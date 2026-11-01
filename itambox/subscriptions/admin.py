from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from core.forms import TenantScopedAdminFormMixin

from .models import Subscription, SubscriptionAssignment


class SubscriptionAssignmentInline(admin.TabularInline):
    model = SubscriptionAssignment
    extra = 0
    readonly_fields = ("assigned_date",)
    raw_id_fields = ("assigned_by",)
    fields = ("content_type", "object_id", "assigned_date", "assigned_by", "notes")


@admin.register(Subscription)
class SubscriptionAdmin(TenantScopedAdminFormMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "supplier",
        "tenant",
        "status",
        "type",
        "renewal_date",
        "renewal_cost",
        "currency",
        "vendor_contract_auto_renews",
    )
    list_filter = (
        "status",
        "type",
        "supplier",
        "tenant",
        "vendor_contract_auto_renews",
        "tags",
        "billing_cycle",
    )
    search_fields = (
        "name",
        "supplier__name",
        "contract_reference",
        "cost_center__name",
        "description",
        "notes",
        "tenant__name",
    )
    readonly_fields = (
        "status",
        "cancellation_date",
        "created_at",
        "updated_at",
        "is_expired_display",
        "days_until_renewal_display",
    )
    raw_id_fields = ("supplier", "owner", "tenant", "cost_center")
    filter_horizontal = ("tags",)
    inlines = [SubscriptionAssignmentInline]
    date_hierarchy = "renewal_date"
    prepopulated_fields = {"slug": ("name",)}

    fieldsets = (
        (_("Identity"), {"fields": ("name", "slug", "supplier", "type", "status", "tenant")}),
        (
            _("Dates & Terms"),
            {
                "fields": (
                    "start_date",
                    "renewal_date",
                    "term_months",
                    "vendor_contract_auto_renews",
                    "cancellation_date",
                )
            },
        ),
        (_("Costs"), {"fields": ("renewal_cost", "currency", "billing_cycle", "cost_center")}),
        (_("Details"), {"fields": ("licensed_quantity", "contract_reference", "linked_contract", "owner")}),
        (_("Notes"), {"fields": ("description", "notes", "tags")}),
        (_("Computed"), {"fields": ("created_at", "updated_at", "is_expired_display", "days_until_renewal_display")}),
    )

    @admin.display(description=_("Expired?"), boolean=True, ordering="renewal_date")
    def is_expired_display(self, obj):
        return obj.is_expired

    @admin.display(description=_("Days Until Renewal"))
    def days_until_renewal_display(self, obj):
        days = obj.days_until_renewal
        if days is None:
            return _("Not set")
        if days >= 0:
            return ngettext("%(count)s day", "%(count)s days", days) % {"count": days}
        count = abs(days)
        return ngettext("%(count)s day overdue", "%(count)s days overdue", count) % {"count": count}

    def get_actions(self, request):
        actions = super().get_actions(request)
        return actions

    @admin.action(description=_("Mark selected as Expired"))
    def mark_expired(self, request, queryset):
        # Save per-instance so each status change is change-logged
        # (QuerySet.update() bypasses ChangeLoggingMixin.save()).
        updated = 0
        for subscription in queryset:
            subscription.expire()
            updated += 1
        self.message_user(
            request,
            ngettext(
                "%(count)s subscription marked as expired.",
                "%(count)s subscriptions marked as expired.",
                updated,
            )
            % {"count": updated},
        )

    @admin.action(description=_("Mark selected as Cancelled"))
    def mark_cancelled(self, request, queryset):

        updated = 0
        for subscription in queryset:
            subscription.cancel(cancellation_date=timezone.now().date())
            updated += 1
        self.message_user(
            request,
            ngettext("%(count)s subscription cancelled.", "%(count)s subscriptions cancelled.", updated)
            % {"count": updated},
        )


@admin.register(SubscriptionAssignment)
class SubscriptionAssignmentAdmin(TenantScopedAdminFormMixin, admin.ModelAdmin):
    list_display = ("subscription", "content_type", "object_id", "assigned_date", "assigned_by")
    list_filter = ("content_type", "subscription__supplier", "subscription__status")
    search_fields = ("subscription__name", "notes")
    raw_id_fields = ("subscription", "assigned_by")
    readonly_fields = ("assigned_date",)
    autocomplete_fields = ("subscription",)
