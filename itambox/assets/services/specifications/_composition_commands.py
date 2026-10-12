"""Locked composition replacement commands for the canonical specification seam."""

from __future__ import annotations

from collections.abc import Sequence

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction

from assets.models.catalog import AssetType, AssetTypeFieldset, Category, CategoryDefaultFieldset
from assets.services.specifications.contracts import (
    AssetTypeId,
    CategoryId,
    CommandRejectedDTO,
    DefinitionRevision,
    ExplicitFieldsetSelectionDTO,
    OwnerChangedDTO,
    OwnerMutationResult,
    OwnerNoOpDTO,
    OwnerRefDTO,
    ResourceRevision,
    SpecificationPatchDTO,
)
from assets.services.specifications.loader import fieldset_ids_for_identities
from assets.services.specifications.locking import catalogue_transaction_lock
from extras.services.specifications.composition import SpecificationDefinitionError
from extras.services.specifications.contracts import QualifiedIdentity
from organization.services.access_scope import ActorContextDTO
from organization.services.catalogue_authorization import has_provider_catalogue_permission

from ._command_support import (
    issue,
    json_values_equal,
    load_effective_definition,
    load_prospective_definition,
    map_structure_error,
    normalize_patch,
    positive_id,
    rejected,
    reload_actor,
    resource_revision_for_owner,
    revision_string,
    save_owner_in_savepoint,
    stale_revision_issues,
    stored_values_for,
    unavailable,
)

_DEFAULT_DB = DEFAULT_DB_ALIAS
_TYPE_PERMISSION = "change_assettype"
_CATEGORY_PERMISSION = "change_category"
_TARGET_KIND = "asset_type"


def _validate_selection_inputs(
    *,
    actor: ActorContextDTO,
    owner_id: int,
    fieldsets: ExplicitFieldsetSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision | None,
    patch: SpecificationPatchDTO | None,
) -> None:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")
    if not isinstance(fieldsets, ExplicitFieldsetSelectionDTO):
        raise TypeError("fieldsets must be an ExplicitFieldsetSelectionDTO")
    positive_id(owner_id, "owner ID")
    revision_string(expected_resource_revision, "expected_resource_revision")
    if expected_definition_revision is not None:
        revision_string(expected_definition_revision, "expected_definition_revision")
    if patch is not None and not isinstance(patch, SpecificationPatchDTO):
        raise TypeError("patch must be a SpecificationPatchDTO")


def _current_membership_rows(owner_model: type[object], owner_id: int, *, using: str) -> tuple[dict[str, int], ...]:
    if owner_model is AssetType:
        through = AssetTypeFieldset
        owner_field = "asset_type_id"
    else:
        through = CategoryDefaultFieldset
        owner_field = "category_id"
    return tuple(
        through.objects.using(using)
        .filter(**{owner_field: owner_id})
        .order_by("position", "fieldset_id", "pk")
        .values("pk", "fieldset_id", "position")
    )


def _reference_rejection(owner_ref: OwnerRefDTO) -> CommandRejectedDTO:
    return rejected(
        owner_ref,
        issue("REFERENCE_CONFLICT", message_key="specifications.reference_conflict"),
    )


def _validate_proposed_graph(
    *,
    graph,
    identities: Sequence[str],
    owner_ref: OwnerRefDTO,
) -> CommandRejectedDTO | None:
    """Reject unresolvable Fieldset selections.

    Selection and target applicability are separate concerns: a Field's
    target decides *where* it contributes, never whether its Fieldset may be
    selected.  The resolver therefore decides per ``target_kind`` which
    members contribute — sibling members targeting other kinds stay valid
    inside the same Fieldset, including a Fieldset whose members all target
    the Asset kind and whose Fields contribute only to Asset specifications.
    This validation owns structure, existence, lifecycle, and activation: an
    empty Fieldset stays selectable (it contributes nothing), and every member must resolve to an existing active, composed
    Field.
    """
    fields_by_identity = {str(field.identity): field for field in graph.fields_by_key.values()}
    for identity in identities:
        fieldset = graph.fieldsets_by_identity.get(QualifiedIdentity(identity))
        if fieldset is None or fieldset.lifecycle != "active":
            return _reference_rejection(owner_ref)
        for membership in fieldset.field_memberships:
            field = fields_by_identity.get(str(membership.field_identity))
            if field is None or field.lifecycle != "active" or field.activation != "composed":
                return _reference_rejection(owner_ref)
    return None


def _lock_type_owner(type_id: int):
    return (
        AssetType.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=type_id, deleted_at__isnull=True)
        .order_by("pk")
        .first()
    )


def _lock_category_owner(category_id: int):
    return (
        Category.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=category_id, deleted_at__isnull=True)
        .order_by("pk")
        .first()
    )


def _definition_for_current_type(owner: AssetType, owner_ref: OwnerRefDTO):
    try:
        stored_values = stored_values_for(owner)
        definition, definitions = load_effective_definition(
            owner.pk,
            _TARGET_KIND,
            tuple(stored_values),
        )
    except (SpecificationDefinitionError, ValueError):
        return map_structure_error(owner_ref)
    return stored_values, definition, definitions


def _proposed_definition(
    *,
    identities: Sequence[str],
    stored_values: dict[str, object],
    owner_ref: OwnerRefDTO,
):
    try:
        definition, definitions, graph = load_prospective_definition(
            identities,
            _TARGET_KIND,
            tuple(stored_values),
        )
    except (SpecificationDefinitionError, ValueError):
        return _reference_rejection(owner_ref)
    graph_rejection = _validate_proposed_graph(
        graph=graph,
        identities=identities,
        owner_ref=owner_ref,
    )
    if graph_rejection is not None:
        return graph_rejection
    return definition, definitions, graph


def _persist_replacement(
    *,
    owner,
    owner_model: type[object],
    owner_id: int,
    fieldset_ids: Sequence[int],
    values_changed: bool,
    proposed_values: dict[str, object],
    membership_changed: bool,
    actor,
    owner_ref: OwnerRefDTO,
) -> CommandRejectedDTO | None:
    if not membership_changed and not values_changed:
        return None

    if hasattr(owner, "snapshot"):
        owner.snapshot()

    through = AssetTypeFieldset if owner_model is AssetType else CategoryDefaultFieldset
    owner_field = "asset_type_id" if owner_model is AssetType else "category_id"
    try:
        with transaction.atomic(using=_DEFAULT_DB):
            if membership_changed:
                through.objects.using(_DEFAULT_DB).filter(**{owner_field: owner_id}).delete()
                through.objects.using(_DEFAULT_DB).bulk_create(
                    [
                        through(
                            **{
                                owner_field: owner_id,
                                "fieldset_id": fieldset_id,
                                "position": position,
                            }
                        )
                        for position, fieldset_id in enumerate(fieldset_ids, start=1)
                    ]
                )
            update_fields: list[str] = []
            if values_changed:
                update_fields.append("custom_field_data")
            update_fields.append("updated_at")
            save_owner_in_savepoint(
                owner,
                actor,
                using=_DEFAULT_DB,
                update_fields=update_fields,
                custom_field_data=proposed_values if values_changed else None,
            )
    except (ValidationError, IntegrityError):
        return rejected(
            owner_ref,
            issue("REFERENCE_CONFLICT", message_key="specifications.reference_conflict"),
        )
    return None


def _finish_type_mutation(
    *,
    owner,
    type_id: int,
    owner_ref: OwnerRefDTO,
    actual_resource_revision: ResourceRevision,
    current_definition,
    proposed_definition,
    proposed_values: dict[str, object],
    desired_ids: Sequence[int],
    actor,
) -> OwnerMutationResult:
    current_rows = _current_membership_rows(AssetType, type_id, using=_DEFAULT_DB)
    desired_rows = tuple((fieldset_id, position) for position, fieldset_id in enumerate(desired_ids, start=1))
    current_pairs = tuple((row["fieldset_id"], row["position"]) for row in current_rows)
    membership_changed = current_pairs != desired_rows
    values_changed = not json_values_equal(owner.custom_field_data, proposed_values)
    if not membership_changed and not values_changed:
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=actual_resource_revision,
            definition_revision=current_definition.revision,
        )

    rejection = _persist_replacement(
        owner=owner,
        owner_model=AssetType,
        owner_id=type_id,
        fieldset_ids=desired_ids,
        values_changed=values_changed,
        proposed_values=proposed_values,
        membership_changed=membership_changed,
        actor=actor,
        owner_ref=owner_ref,
    )
    if rejection is not None:
        return rejection
    return OwnerChangedDTO(
        outcome="changed",
        owner=owner_ref,
        resource_revision=resource_revision_for_owner(owner),
        definition_revision=proposed_definition.revision,
    )


def _set_type_locked(
    *,
    actor: ActorContextDTO,
    type_id: int,
    fieldsets: ExplicitFieldsetSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    owner = _lock_type_owner(type_id)
    owner_ref = OwnerRefDTO("asset_type", type_id)
    if owner is None:
        return unavailable()

    actor_model = reload_actor(actor)
    if actor_model is None or not has_provider_catalogue_permission(
        actor_model,
        f"{AssetType._meta.app_label}.{_TYPE_PERMISSION}",
    ):
        return unavailable()

    current_plan = _definition_for_current_type(owner, owner_ref)
    if isinstance(current_plan, CommandRejectedDTO):
        return current_plan
    stored_values, current_definition, _current_definitions = current_plan
    actual_resource_revision = resource_revision_for_owner(owner)
    if expected_resource_revision != actual_resource_revision:
        # A stale owner rejects the request before any fieldset reference is
        # evaluated. Definition staleness is still reported when it can be
        # determined: the client's preview revision is compared against the
        # prospective definition the server builds from the same identities,
        # falling back to the current definition only when the identities are
        # not resolvable (the reference check itself stays suppressed).
        definition_revision = current_definition.revision
        prospective_plan = _proposed_definition(
            identities=fieldsets.identities,
            stored_values=stored_values,
            owner_ref=owner_ref,
        )
        if not isinstance(prospective_plan, CommandRejectedDTO):
            prospective_definition, _prospective_definitions, _prospective_graph = prospective_plan
            definition_revision = prospective_definition.revision
        revision_issues = stale_revision_issues(
            expected_resource_revision=expected_resource_revision,
            actual_resource_revision=actual_resource_revision,
            expected_definition_revision=expected_definition_revision,
            actual_definition_revision=definition_revision,
        )
        return rejected(owner_ref, *revision_issues)

    proposed_plan = _proposed_definition(
        identities=fieldsets.identities,
        stored_values=stored_values,
        owner_ref=owner_ref,
    )
    if isinstance(proposed_plan, CommandRejectedDTO):
        return proposed_plan
    proposed_definition, proposed_definitions, _graph = proposed_plan
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=proposed_definition.revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)
    normalized = normalize_patch(
        patch,
        proposed_definitions,
        stored_values,
        operation="composition_edit",
    )
    if isinstance(normalized, tuple):
        return rejected(owner_ref, *normalized)
    proposed_values = dict(normalized.stored_values)

    proposed_ids = fieldset_ids_for_identities(fieldsets.identities, using=_DEFAULT_DB)
    if any(fieldset_id is None for fieldset_id in proposed_ids):
        return _reference_rejection(owner_ref)
    desired_ids = tuple(fieldset_id for fieldset_id in proposed_ids if fieldset_id is not None)
    return _finish_type_mutation(
        owner=owner,
        type_id=type_id,
        owner_ref=owner_ref,
        actual_resource_revision=actual_resource_revision,
        current_definition=current_definition,
        proposed_definition=proposed_definition,
        proposed_values=proposed_values,
        desired_ids=desired_ids,
        actor=actor_model,
    )


def _set_category_locked(
    *,
    actor: ActorContextDTO,
    category_id: int,
    fieldsets: ExplicitFieldsetSelectionDTO,
    expected_resource_revision: ResourceRevision,
) -> OwnerMutationResult:
    owner = _lock_category_owner(category_id)
    owner_ref = OwnerRefDTO("category", category_id)
    if owner is None:
        return unavailable()

    actor_model = reload_actor(actor)
    if actor_model is None or not has_provider_catalogue_permission(
        actor_model,
        f"{Category._meta.app_label}.{_CATEGORY_PERMISSION}",
    ):
        return unavailable()

    actual_resource_revision = resource_revision_for_owner(owner)
    if expected_resource_revision != actual_resource_revision:
        return rejected(
            owner_ref,
            issue("STALE_RESOURCE", message_key="specifications.stale_resource"),
        )

    proposed_plan = _proposed_definition(
        identities=fieldsets.identities,
        stored_values={},
        owner_ref=owner_ref,
    )
    if isinstance(proposed_plan, CommandRejectedDTO):
        return proposed_plan
    proposed_definition, _proposed_definitions, _graph = proposed_plan
    proposed_ids = fieldset_ids_for_identities(fieldsets.identities, using=_DEFAULT_DB)
    if any(fieldset_id is None for fieldset_id in proposed_ids):
        return _reference_rejection(owner_ref)
    desired_ids = tuple(fieldset_id for fieldset_id in proposed_ids if fieldset_id is not None)
    current_rows = _current_membership_rows(Category, category_id, using=_DEFAULT_DB)
    desired_rows = tuple((fieldset_id, position) for position, fieldset_id in enumerate(desired_ids, start=1))
    current_pairs = tuple((row["fieldset_id"], row["position"]) for row in current_rows)
    membership_changed = current_pairs != desired_rows
    if not membership_changed:
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=actual_resource_revision,
            definition_revision=proposed_definition.revision,
        )

    rejection = _persist_replacement(
        owner=owner,
        owner_model=Category,
        owner_id=category_id,
        fieldset_ids=desired_ids,
        values_changed=False,
        proposed_values={},
        membership_changed=True,
        actor=actor_model,
        owner_ref=owner_ref,
    )
    if rejection is not None:
        return rejection
    return OwnerChangedDTO(
        outcome="changed",
        owner=owner_ref,
        resource_revision=resource_revision_for_owner(owner),
        definition_revision=proposed_definition.revision,
    )


def set_asset_type_composition(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    fieldsets: ExplicitFieldsetSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    """Replace one Asset Type's ordered Fieldset composition and optional values."""
    _validate_selection_inputs(
        actor=actor,
        owner_id=asset_type_id,
        fieldsets=fieldsets,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        patch=patch,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(exclusive=True, using=_DEFAULT_DB):
            return _set_type_locked(
                actor=actor,
                type_id=asset_type_id,
                fieldsets=fieldsets,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=patch,
            )


def set_category_defaults(
    *,
    actor: ActorContextDTO,
    category_id: CategoryId,
    expected_resource_revision: ResourceRevision,
    fieldsets: ExplicitFieldsetSelectionDTO,
) -> OwnerMutationResult:
    """Replace one Category's ordered default Fieldset list only."""
    _validate_selection_inputs(
        actor=actor,
        owner_id=category_id,
        fieldsets=fieldsets,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=None,
        patch=None,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(exclusive=True, using=_DEFAULT_DB):
            return _set_category_locked(
                actor=actor,
                category_id=category_id,
                fieldsets=fieldsets,
                expected_resource_revision=expected_resource_revision,
            )


__all__ = ["set_asset_type_composition", "set_category_defaults"]
