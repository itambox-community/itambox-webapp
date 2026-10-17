from __future__ import annotations

import strawberry
import strawberry_django
from django.contrib.contenttypes.models import ContentType

from assets.schema import SupplierNode, TenantNode
from core.graphql_choice_enums import choice_enum
from core.graphql_scalars import BigInt
from core.graphql_utils import check_permission, paginate_queryset, scoped_queryset

from .models import Subscription, SubscriptionAssignment


@strawberry_django.type(ContentType, name="ContentTypeNode", fields=["id", "app_label", "model"])
class ContentTypeNode:
    pass


SubscriptionTypeChoices = choice_enum(Subscription, "type")
SubscriptionStatusChoices = choice_enum(Subscription, "status")
SubscriptionCurrencyChoices = choice_enum(Subscription, "currency")
SubscriptionBillingCycleChoices = choice_enum(Subscription, "billing_cycle")


@strawberry_django.type(
    Subscription,
    name="SubscriptionNode",
    fields=[
        "id",
        "name",
        "slug",
        "start_date",
        "renewal_date",
        "renewal_cost",
        "term_months",
        "vendor_contract_auto_renews",
        "licensed_quantity",
        "contract_reference",
        "cancellation_date",
        "description",
        "notes",
        "created_at",
        "updated_at",
    ],
)
class SubscriptionNode:
    supplier: SupplierNode
    type: SubscriptionTypeChoices
    status: SubscriptionStatusChoices
    currency: SubscriptionCurrencyChoices | None
    billing_cycle: SubscriptionBillingCycleChoices | None
    tenant: TenantNode | None

    @strawberry.field
    def cost_center_id(self) -> strawberry.ID | None:
        return self.cost_center_id

    @strawberry.field
    def cost_center_name(self) -> str | None:
        return str(self.cost_center) if self.cost_center_id else None

    @strawberry.field(deprecation_reason="Use vendorContractAutoRenews; autoRenewal is removed in 2.0.")
    def auto_renewal(self) -> bool | None:
        return self.vendor_contract_auto_renews


@strawberry_django.type(
    SubscriptionAssignment,
    name="SubscriptionAssignmentNode",
    fields=["id", "assigned_date", "notes", "created_at", "updated_at"],
)
class SubscriptionAssignmentNode:
    subscription: SubscriptionNode
    content_type: ContentTypeNode | None
    object_id: BigInt


SUBSCRIPTION_SORTABLE_FIELDS = {
    "name",
    "-name",
    "renewal_date",
    "-renewal_date",
    "renewal_cost",
    "-renewal_cost",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}

_ASSIGNMENT_RELATED = ("subscription", "subscription__supplier", "assigned_by", "content_type")


@strawberry.type
class Query:
    @strawberry.field
    def subscriptions(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
        status: str | None = None,
        type: str | None = None,  # noqa: A002 - public argument name
    ) -> list[SubscriptionNode | None] | None:
        check_permission(info, "subscriptions.view_subscription")
        # TenantScopingSoftDeleteManager handles tenant scoping and active filtering.
        qs = (
            scoped_queryset(info, Subscription.objects)
            .select_related("supplier", "linked_contract", "tenant", "owner")
            .all()
        )
        for key, val in (("name", name), ("status", status), ("type", type)):
            if val is not None:
                qs = qs.filter(**{key: val})
        if sort_by and sort_by in SUBSCRIPTION_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def subscription(self, info: strawberry.Info, id: strawberry.ID) -> SubscriptionNode | None:
        check_permission(info, "subscriptions.view_subscription")
        try:
            return (
                scoped_queryset(info, Subscription.objects)
                .select_related("supplier", "linked_contract", "tenant", "owner")
                .get(pk=id)
            )
        except Subscription.DoesNotExist:
            return None

    @strawberry.field
    def subscription_assignments(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        subscription_id: strawberry.ID | None = None,
    ) -> list[SubscriptionAssignmentNode | None] | None:
        check_permission(info, "subscriptions.view_subscriptionassignment")
        # SubscriptionAssignment has no direct tenant field, scope via its subscription
        qs = scoped_queryset(info, SubscriptionAssignment.objects).select_related(*_ASSIGNMENT_RELATED)
        if subscription_id is not None:
            qs = qs.filter(subscription_id=subscription_id)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def subscription_assignment(self, info: strawberry.Info, id: strawberry.ID) -> SubscriptionAssignmentNode | None:
        check_permission(info, "subscriptions.view_subscriptionassignment")
        try:
            return scoped_queryset(info, SubscriptionAssignment.objects).select_related(*_ASSIGNMENT_RELATED).get(pk=id)
        except SubscriptionAssignment.DoesNotExist:
            return None
