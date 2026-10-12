"""Canonical locked value commands for Asset Types and Assets.

This is intentionally a small public surface.  Both commands use the same
PostgreSQL catalogue transaction lock and the existing pure specification
codec/resolver; no alternate raw-JSON writer is provided here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction

from assets.models.asset import Asset
from assets.models.catalog import AssetType
from assets.services.specifications.contracts import (
    AssetId,
    AssetTypeId,
    CommandRejectedDTO,
    DefinitionRevision,
    DestinationAssetTypeSelectionDTO,
    OwnerChangedDTO,
    OwnerMutationResult,
    OwnerNoOpDTO,
    OwnerRefDTO,
    ResourceRevision,
    SpecificationPatchDTO,
)
from assets.services.specifications.locking import catalogue_transaction_lock
from extras.services.specifications.composition import SpecificationDefinitionError
from organization.services.access_scope import (
    AccessScopeResolvedDTO,
    ActorContextDTO,
    ResolvedAccessAuthorizationDTO,
    reauthorize_access_scope,
)
from organization.services.catalogue_authorization import has_provider_catalogue_permission

from ._command_support import (
    json_values_equal,
    load_effective_definition,
    map_reference_error,
    map_structure_error,
    normalize_patch,
    positive_id,
    rejected,
    reload_actor,
    resource_revision_for_owner,
    revision_string,
    save_owner_in_savepoint,
    stale_plan_issue,
    stale_revision_issues,
    stored_values_for,
    unavailable,
)
from ._composition_commands import set_asset_type_composition, set_category_defaults
from ._create_commands import (
    apply_category_defaults,
    create_asset_type,
    preview_apply_category_defaults,
    preview_asset_type_create,
)
from ._history_commands import (
    cleanup_asset_history,
    cleanup_asset_type_history,
    preview_asset_history_cleanup,
    preview_asset_type_history_cleanup,
)

_DEFAULT_DB = DEFAULT_DB_ALIAS
_ASSET_CHANGE_PERMISSION = "assets.change_asset"


def _validate_type_command_inputs(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> int:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")
    if not isinstance(patch, SpecificationPatchDTO):
        raise TypeError("patch must be a SpecificationPatchDTO")
    positive_id(asset_type_id, "Asset Type ID")
    revision_string(expected_resource_revision, "expected_resource_revision")
    revision_string(expected_definition_revision, "expected_definition_revision")
    return asset_type_id


def _type_definition_or_rejection(
    *,
    owner: AssetType,
    target_kind: str,
    stored_values: dict[str, object],
) -> tuple[object, object] | CommandRejectedDTO:
    try:
        return load_effective_definition(owner.pk, target_kind, tuple(stored_values))  # type: ignore[arg-type]
    except (SpecificationDefinitionError, ValueError):
        return map_structure_error(OwnerRefDTO("asset_type", owner.pk))


def _type_plan_or_rejection(
    owner: AssetType,
    owner_ref: OwnerRefDTO,
) -> tuple[dict[str, object], object, object] | CommandRejectedDTO:
    try:
        stored_values = stored_values_for(owner)
        definition_result = _type_definition_or_rejection(
            owner=owner,
            target_kind="asset_type",
            stored_values=stored_values,
        )
    except ValueError:
        return map_structure_error(owner_ref)
    if isinstance(definition_result, CommandRejectedDTO):
        return definition_result
    definition, definitions = definition_result
    return stored_values, definition, definitions


def _save_owner_or_rejection(
    owner: Asset | AssetType,
    actor: Any,
    owner_ref: OwnerRefDTO,
    *,
    update_fields: list[str] | tuple[str, ...],
    custom_field_data: Mapping[str, object] | None = None,
) -> CommandRejectedDTO | None:
    try:
        save_owner_in_savepoint(
            owner,
            actor,
            using=_DEFAULT_DB,
            update_fields=update_fields,
            custom_field_data=custom_field_data,
        )
    except ValidationError as error:
        return map_reference_error(owner_ref, error)
    return None


def _update_asset_type_locked(
    *,
    actor: ActorContextDTO,
    type_id: int,
    owner_ref: OwnerRefDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    owner = AssetType.objects.using(_DEFAULT_DB).select_for_update().filter(pk=type_id, deleted_at__isnull=True).first()
    if owner is None:
        return unavailable()

    actor_model = reload_actor(actor)
    if actor_model is None or not has_provider_catalogue_permission(actor_model, "assets.change_assettype"):
        return unavailable()

    plan = _type_plan_or_rejection(owner, owner_ref)
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, definition, definitions = plan
    actual_resource_revision = resource_revision_for_owner(owner)
    actual_definition_revision = definition.revision
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=actual_definition_revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)

    normalized = normalize_patch(
        patch,
        definitions,
        stored_values,
        operation="specification_edit",
    )
    if isinstance(normalized, tuple):
        return rejected(owner_ref, *normalized)
    proposed_values = {str(key): value for key, value in normalized.stored_values.items()}
    if json_values_equal(owner.custom_field_data, proposed_values):
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=actual_resource_revision,
            definition_revision=actual_definition_revision,
        )

    rejection = _save_owner_or_rejection(
        owner,
        actor_model,
        owner_ref,
        update_fields=("custom_field_data", "updated_at"),
        custom_field_data=proposed_values,
    )
    if rejection is not None:
        return rejection
    return OwnerChangedDTO(
        outcome="changed",
        owner=owner_ref,
        resource_revision=resource_revision_for_owner(owner),
        definition_revision=actual_definition_revision,
    )


def update_asset_type_specifications(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    """Apply one validated set/clear patch to a globally shared Asset Type."""
    type_id = _validate_type_command_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        patch=patch,
    )
    owner_ref = OwnerRefDTO("asset_type", type_id)

    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _update_asset_type_locked(
                actor=actor,
                type_id=type_id,
                owner_ref=owner_ref,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=patch,
            )


def _validate_asset_command_inputs(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    destination: DestinationAssetTypeSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> int:
    if not isinstance(authorization, ResolvedAccessAuthorizationDTO):
        raise TypeError("authorization must be a ResolvedAccessAuthorizationDTO")
    if not isinstance(destination, DestinationAssetTypeSelectionDTO):
        raise TypeError("destination must be a DestinationAssetTypeSelectionDTO")
    if not isinstance(patch, SpecificationPatchDTO):
        raise TypeError("patch must be a SpecificationPatchDTO")
    positive_id(asset_id, "Asset ID")
    revision_string(expected_resource_revision, "expected_resource_revision")
    revision_string(expected_definition_revision, "expected_definition_revision")
    return asset_id


def _reauthorize_asset_update(
    authorization: ResolvedAccessAuthorizationDTO,
    owner: Asset,
    owner_ref: OwnerRefDTO,
) -> object | CommandRejectedDTO:
    fresh_scope = reauthorize_access_scope(authorization)
    if not isinstance(fresh_scope, AccessScopeResolvedDTO):
        return unavailable()
    if (
        authorization.request.operation != "update_asset_specifications"
        or authorization.request.required_permission != _ASSET_CHANGE_PERMISSION
    ):
        return unavailable()
    if owner.tenant_id is None or owner.tenant_id not in fresh_scope.access_scope.authorized_tenant_ids:
        return unavailable()
    if fresh_scope.access_scope.access_scope_fingerprint != authorization.initial_scope.access_scope_fingerprint:
        return rejected(owner_ref, stale_plan_issue())
    actor_model = reload_actor(authorization.actor)
    if actor_model is None:
        return unavailable()
    return actor_model


def _asset_destination_type_id(
    owner: Asset,
    destination: DestinationAssetTypeSelectionDTO,
) -> int | None | CommandRejectedDTO:
    destination_type_id = owner.asset_type_id if destination.presence == "keep_current" else destination.asset_type_id
    if destination_type_id is None:
        return None
    destination_type = (
        AssetType.objects.using(_DEFAULT_DB).filter(pk=destination_type_id, deleted_at__isnull=True).first()
    )
    if destination_type is None:
        return unavailable()
    return destination_type_id


def _asset_plan_or_rejection(
    owner: Asset,
    destination_type_id: int | None,
    owner_ref: OwnerRefDTO,
) -> tuple[dict[str, object], object, object] | CommandRejectedDTO:
    try:
        stored_values = stored_values_for(owner)
        definition, definitions = load_effective_definition(
            destination_type_id,
            "asset",
            tuple(stored_values),
        )
    except (SpecificationDefinitionError, ValueError):
        return map_structure_error(owner_ref)
    return stored_values, definition, definitions


def _update_asset_locked(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    owner_id: int,
    destination: DestinationAssetTypeSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    owner = Asset.objects.using(_DEFAULT_DB).select_for_update().filter(pk=owner_id, deleted_at__isnull=True).first()
    if owner is None:
        return unavailable()
    owner_ref = OwnerRefDTO("asset", owner.pk)

    actor_result = _reauthorize_asset_update(authorization, owner, owner_ref)
    if isinstance(actor_result, CommandRejectedDTO):
        return actor_result

    destination_result = _asset_destination_type_id(owner, destination)
    if isinstance(destination_result, CommandRejectedDTO):
        return destination_result
    destination_type_id = destination_result
    plan = _asset_plan_or_rejection(owner, destination_type_id, owner_ref)
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, definition, definitions = plan
    actual_resource_revision = resource_revision_for_owner(owner)
    actual_definition_revision = definition.revision
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=actual_definition_revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)

    operation = "asset_type_switch" if destination_type_id != owner.asset_type_id else "value_edit"
    normalized = normalize_patch(
        patch,
        definitions,
        stored_values,
        operation=operation,
    )
    if isinstance(normalized, tuple):
        return rejected(owner_ref, *normalized)
    proposed_values = {str(key): value for key, value in normalized.stored_values.items()}
    type_changed = destination_type_id != owner.asset_type_id
    values_changed = not json_values_equal(owner.custom_field_data, proposed_values)
    if not type_changed and not values_changed:
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=actual_resource_revision,
            definition_revision=actual_definition_revision,
        )

    update_fields = ["custom_field_data", "updated_at"]
    if type_changed:
        owner.asset_type_id = destination_type_id
        update_fields.append("asset_type")
    rejection = _save_owner_or_rejection(
        owner,
        actor_result,
        owner_ref,
        update_fields=update_fields,
        custom_field_data=proposed_values,
    )
    if rejection is not None:
        return rejection
    return OwnerChangedDTO(
        outcome="changed",
        owner=owner_ref,
        resource_revision=resource_revision_for_owner(owner),
        definition_revision=actual_definition_revision,
    )


def update_asset_specifications(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    destination: DestinationAssetTypeSelectionDTO,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    """Apply a tenant-authorized Asset value patch, optionally switching Type."""
    owner_id = _validate_asset_command_inputs(
        authorization=authorization,
        asset_id=asset_id,
        destination=destination,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        patch=patch,
    )

    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _update_asset_locked(
                authorization=authorization,
                owner_id=owner_id,
                destination=destination,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                patch=patch,
            )


__all__ = [
    "apply_category_defaults",
    "cleanup_asset_history",
    "cleanup_asset_type_history",
    "create_asset_type",
    "preview_apply_category_defaults",
    "preview_asset_history_cleanup",
    "preview_asset_type_create",
    "preview_asset_type_history_cleanup",
    "set_asset_type_composition",
    "set_category_defaults",
    "update_asset_specifications",
    "update_asset_type_specifications",
]
