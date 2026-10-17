from __future__ import annotations

import strawberry
import strawberry_django

from assets.schema import ManufacturerNode
from core.graphql_choice_enums import choice_enum
from core.graphql_utils import check_permission, paginate_queryset, scoped_queryset

from .models import Software

SOFTWARE_SORTABLE_FIELDS = {
    "name",
    "-name",
    "version",
    "-version",
    "category",
    "-category",
    "created_at",
    "-created_at",
    "updated_at",
    "-updated_at",
}


SoftwareCategoryChoices = choice_enum(Software, "category")
SoftwareLicenseTypeChoices = choice_enum(Software, "license_type")


@strawberry_django.type(
    Software,
    name="SoftwareNode",
    fields=[
        "id",
        "name",
        "version",
        "website",
        "description",
        "created_at",
        "updated_at",
    ],
)
class SoftwareNode:
    manufacturer: ManufacturerNode
    category: SoftwareCategoryChoices | None
    license_type: SoftwareLicenseTypeChoices | None


@strawberry.type
class Query:
    @strawberry.field
    def software_list(
        self,
        info: strawberry.Info,
        limit: int | None = None,
        offset: int | None = None,
        sort_by: str | None = None,
        name: str | None = None,
        version: str | None = None,
    ) -> list[SoftwareNode | None] | None:
        check_permission(info, "software.view_software")
        qs = scoped_queryset(info, Software.objects).select_related("manufacturer").all()
        for key, val in (("name", name), ("version", version)):
            if val is not None:
                qs = qs.filter(**{key: val})
        if sort_by and sort_by in SOFTWARE_SORTABLE_FIELDS:
            qs = qs.order_by(sort_by)
        return list(paginate_queryset(qs, limit, offset))

    @strawberry.field
    def software(self, info: strawberry.Info, id: strawberry.ID) -> SoftwareNode | None:
        check_permission(info, "software.view_software")
        try:
            return scoped_queryset(info, Software.objects).select_related("manufacturer").get(pk=id)
        except Software.DoesNotExist:
            return None
