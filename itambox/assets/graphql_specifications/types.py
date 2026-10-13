"""Explicit Strawberry read types backed by specification DTOs.

These types intentionally expose only the frozen specification vocabulary.  They
do not inherit Django model fields and they never perform ORM work from a Field
or Choice resolver.  Roots are plain DTOs, so resolvers read DTO attributes
directly and the value union converts DTOs explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated

import strawberry

from extras.services.specifications.contracts import (
    FieldDefinitionDTO,
    PersistedFieldsetDTO,
    ResolvedFieldDTO,
)

from .readers import (
    BooleanSpecificationValue,
    ChoiceSpecificationValue,
    DateSpecificationValue,
    DecimalSpecificationValue,
    IntegerSpecificationValue,
    MultiChoiceSpecificationValue,
    NullSpecificationValue,
    TextSpecificationValue,
    UninterpretedSpecificationValue,
    specification_value_for_entry,
)
from .scalars import CursorScalar, DateScalar, DecimalScalar, JSONScalar, SafeInteger


@strawberry.enum(name="SpecificationTarget")
class SpecificationTargetEnum(Enum):
    ASSET_TYPE = "asset_type"
    ASSET = "asset"


@strawberry.enum(name="ScopeMode")
class ScopeModeEnum(Enum):
    TENANT = "tenant"
    TENANT_GROUP = "tenant_group"
    ALL_ACCESSIBLE = "all_accessible"


@strawberry.enum(name="SpecificationFieldType")
class SpecificationFieldTypeEnum(Enum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


@strawberry.enum(name="DefinitionLifecycle")
class DefinitionLifecycleEnum(Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


@strawberry.enum(name="FieldActivation")
class FieldActivationEnum(Enum):
    COMPOSED = "composed"
    GLOBAL = "global"


@strawberry.enum(name="SpecificationEntryState")
class SpecificationEntryStateEnum(Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@strawberry.type(name="Choice")
class ChoiceType:
    key: str
    label: str
    lifecycle: DefinitionLifecycleEnum


@strawberry.type(name="ChoiceSet")
class ChoiceSetType:
    identity: str
    label: str
    resource_revision: str
    lifecycle: DefinitionLifecycleEnum
    choices: list[ChoiceType]


@strawberry.type(name="SpecificationValidation")
class SpecificationValidationType:
    minimum: DecimalScalar | None
    maximum: DecimalScalar | None
    scale: int | None
    max_length: int | None
    max_values: int | None
    regex: str | None
    rule: str | None


@strawberry.type(name="SpecificationField")
class SpecificationFieldNode:
    resource_revision: str
    key: str
    identity: str
    label: str
    help_text: str

    @strawberry.field
    def targets(self) -> list[SpecificationTargetEnum]:
        # ``self`` is the DTO root, not an instance of this class.
        return [SpecificationTargetEnum(value) for value in sorted(self.targets)]  # type: ignore[attr-defined]

    activation: FieldActivationEnum
    field_type: SpecificationFieldTypeEnum
    required: bool
    nullable: bool
    quantity_kind: str | None
    canonical_unit: str | None
    validation: SpecificationValidationType
    lifecycle: DefinitionLifecycleEnum
    choice_set: ChoiceSetType | None

    @strawberry.field
    def sources(self) -> list[str]:
        return [str(i) for i in getattr(self, "contributing_section_identities", ())]


@dataclass(frozen=True)
class FieldsetView:
    definition: PersistedFieldsetDTO
    fields: tuple[FieldDefinitionDTO | ResolvedFieldDTO, ...]


@strawberry.type(name="SpecificationFieldset")
class SpecificationFieldsetType:
    @strawberry.field
    def resource_revision(self) -> str:
        return self.definition.resource_revision  # type: ignore[attr-defined]

    @strawberry.field
    def identity(self) -> str:
        return self.definition.identity  # type: ignore[attr-defined]

    @strawberry.field
    def label(self) -> str:
        return self.definition.label  # type: ignore[attr-defined]

    @strawberry.field
    def description(self) -> str:
        return self.definition.description  # type: ignore[attr-defined]

    @strawberry.field
    def lifecycle(self) -> DefinitionLifecycleEnum:
        return DefinitionLifecycleEnum(self.definition.lifecycle)  # type: ignore[attr-defined]

    @strawberry.field
    def fields(self) -> list[SpecificationFieldNode]:
        return list(self.fields)  # type: ignore[arg-type]


@strawberry.type(name="SpecificationSection")
class SpecificationSectionType:
    identity: str | None
    label: str

    @strawberry.field
    def fields(self) -> list[SpecificationFieldNode]:
        return list(self.fields)  # type: ignore[arg-type]


@strawberry.type(name="SpecificationDefinition")
class SpecificationDefinitionType:
    revision: str

    @strawberry.field
    def target(self) -> SpecificationTargetEnum:
        return SpecificationTargetEnum(self.target_kind)  # type: ignore[attr-defined]

    @strawberry.field
    def sections(self) -> list[SpecificationSectionType]:
        return list(self.rendered_sections)  # type: ignore[attr-defined,arg-type]


@strawberry.type(name="TextSpecificationValue")
class TextSpecificationValueType:
    text: str


@strawberry.type(name="IntegerSpecificationValue")
class IntegerSpecificationValueType:
    integer: SafeInteger


@strawberry.type(name="DecimalSpecificationValue")
class DecimalSpecificationValueType:
    decimal: DecimalScalar


@strawberry.type(name="BooleanSpecificationValue")
class BooleanSpecificationValueType:
    boolean: bool


@strawberry.type(name="DateSpecificationValue")
class DateSpecificationValueType:
    date: DateScalar


@strawberry.type(name="ChoiceSpecificationValue")
class ChoiceSpecificationValueType:
    choice: str


@strawberry.type(name="MultiChoiceSpecificationValue")
class MultiChoiceSpecificationValueType:
    choices: list[str]


@strawberry.type(name="NullSpecificationValue")
class NullSpecificationValueType:
    is_null: bool


@strawberry.type(name="UninterpretedSpecificationValue")
class UninterpretedSpecificationValueType:
    json: JSONScalar


SpecificationValueUnion = Annotated[
    TextSpecificationValueType
    | IntegerSpecificationValueType
    | DecimalSpecificationValueType
    | BooleanSpecificationValueType
    | DateSpecificationValueType
    | ChoiceSpecificationValueType
    | MultiChoiceSpecificationValueType
    | NullSpecificationValueType
    | UninterpretedSpecificationValueType,
    strawberry.union("SpecificationValue"),
]


def _value_type_for(instance: object):
    """Convert a reader value dataclass into the matching GraphQL object."""
    if isinstance(instance, TextSpecificationValue):
        return TextSpecificationValueType(text=instance.text)
    if isinstance(instance, IntegerSpecificationValue):
        return IntegerSpecificationValueType(integer=instance.integer)
    if isinstance(instance, DecimalSpecificationValue):
        return DecimalSpecificationValueType(decimal=instance.decimal)
    if isinstance(instance, BooleanSpecificationValue):
        return BooleanSpecificationValueType(boolean=instance.boolean)
    if isinstance(instance, DateSpecificationValue):
        return DateSpecificationValueType(date=instance.date)
    if isinstance(instance, ChoiceSpecificationValue):
        return ChoiceSpecificationValueType(choice=instance.choice)
    if isinstance(instance, MultiChoiceSpecificationValue):
        return MultiChoiceSpecificationValueType(choices=list(instance.choices))
    if isinstance(instance, NullSpecificationValue):
        return NullSpecificationValueType(is_null=instance.is_null)
    if isinstance(instance, UninterpretedSpecificationValue):
        return UninterpretedSpecificationValueType(json=instance.json)
    return None


@strawberry.type(name="SpecificationEntry")
class SpecificationEntryType:
    key: str
    definition: SpecificationFieldNode | None
    state: SpecificationEntryStateEnum

    @strawberry.field
    def reason_codes(self) -> list[str]:
        return list(self.reason_codes)  # type: ignore[arg-type]

    @strawberry.field
    def value(self) -> SpecificationValueUnion:
        return _value_type_for(specification_value_for_entry(self))  # type: ignore[arg-type]


@strawberry.type(name="UserError")
class UserErrorType:
    code: str
    path: list[str]
    field_key: str | None
    message: str


@strawberry.type(name="PageInfo")
class PageInfoType:
    end_cursor: CursorScalar | None
    has_next_page: bool


@dataclass(frozen=True)
class AssetTypeEdgeView:
    cursor: str
    node: object


@dataclass(frozen=True)
class AssetTypeConnectionView:
    edges: tuple[AssetTypeEdgeView, ...]
    page_info: PageInfoType


@dataclass(frozen=True)
class SpecificationFieldEdgeView:
    cursor: str
    node: FieldDefinitionDTO


@dataclass(frozen=True)
class SpecificationFieldConnectionView:
    edges: tuple[SpecificationFieldEdgeView, ...]
    page_info: PageInfoType


@strawberry.type(name="SpecificationFieldEdge")
class SpecificationFieldEdgeType:
    cursor: CursorScalar
    node: SpecificationFieldNode


@strawberry.type(name="SpecificationFieldConnection")
class SpecificationFieldConnectionType:
    edges: list[SpecificationFieldEdgeType]
    page_info: PageInfoType


__all__ = [
    "BooleanSpecificationValue",
    "ChoiceSetType",
    "ChoiceSpecificationValue",
    "ChoiceSpecificationValueType",
    "ChoiceType",
    "CursorScalar",
    "DateScalar",
    "DateSpecificationValue",
    "DateSpecificationValueType",
    "DecimalSpecificationValue",
    "DecimalSpecificationValueType",
    "DefinitionLifecycleEnum",
    "FieldActivationEnum",
    "FieldsetView",
    "IntegerSpecificationValue",
    "IntegerSpecificationValueType",
    "JSONScalar",
    "MultiChoiceSpecificationValue",
    "MultiChoiceSpecificationValueType",
    "NullSpecificationValue",
    "NullSpecificationValueType",
    "PageInfoType",
    "SpecificationDefinitionType",
    "SpecificationEntryStateEnum",
    "SpecificationEntryType",
    "SpecificationFieldConnectionType",
    "SpecificationFieldEdgeType",
    "SpecificationFieldNode",
    "SpecificationFieldTypeEnum",
    "SpecificationFieldsetType",
    "SpecificationSectionType",
    "ScopeModeEnum",
    "SpecificationTargetEnum",
    "SpecificationValidationType",
    "SpecificationValueUnion",
    "TextSpecificationValue",
    "TextSpecificationValueType",
    "UninterpretedSpecificationValue",
    "UninterpretedSpecificationValueType",
    "UserErrorType",
]
