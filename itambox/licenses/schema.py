from __future__ import annotations

import strawberry
import strawberry_django

from assets.schema import SupplierNode, TenantNode
from core.graphql_choice_enums import choice_enum
from core.graphql_utils import check_permission, paginate_queryset, scoped_queryset, scoped_relation
from software.schema import SoftwareNode

from .models import License

LICENSE_SORTABLE_FIELDS = {
    "name",
    "-name",
    "purchase_date",
    "-purchase_date",
    "expiration_date",
    "-expiration_date",
    "seats",
    "-seats",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}

_RELATED = ("software", "software__manufacturer", "supplier", "tenant")


LicenseTypeChoices = choice_enum(License, "license_type")


@strawberry_django.type(
    License,
    name="LicenseNode",
    fields=[
        "id",
        "name",
        "seats",
        "purchase_date",
        "order_number",
        "expiration_date",
        "created_at",
        "updated_at",
    ],
)
class LicenseNode:
    license_type: LicenseTypeChoices
    tenant: TenantNode | None

    @strawberry.field
    def software(self, info: strawberry.Info) -> SoftwareNode | None:
        # Decision (#1055): the license entry point does not authorize the
        # software record; the direct software roots require this permission.
        # `License.clean()` constrains `software` to the license's own tenant,
        # but anchor the check on THIS software's own tenant anyway: an import
        # or legacy row can predate that invariant, and an ambient check would
        # pass on the caller's union of tenants (#1088).
        return scoped_relation(info, self.software, "software.view_software")

    @strawberry.field
    def supplier(self, info: strawberry.Info) -> SupplierNode | None:
        return scoped_relation(info, self.supplier, "assets.view_supplier")


@strawberry.type
class Query:
    @strawberry.field
    def licenses(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
    ) -> list[LicenseNode | None] | None:
        check_permission(info, "licenses.view_license")
        qs = scoped_queryset(info, License.objects, required_permission="licenses.view_license").select_related(
            *_RELATED
        )
        if name is not None:
            qs = qs.filter(name=name)
        if sort_by and sort_by in LICENSE_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def license(self, info: strawberry.Info, id: strawberry.ID) -> LicenseNode | None:
        check_permission(info, "licenses.view_license")
        try:
            return (
                scoped_queryset(info, License.objects, required_permission="licenses.view_license")
                .select_related(*_RELATED)
                .get(pk=id)
            )
        except License.DoesNotExist:
            return None
