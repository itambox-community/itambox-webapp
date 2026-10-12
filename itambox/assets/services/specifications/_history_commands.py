"""Previewed, authorized removal of stored specification history entries."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction

from assets.models.asset import Asset
from assets.models.catalog import AssetType
from assets.services.specifications.contracts import (
    AssetId,
    AssetTypeId,
    CommandRejectedDTO,
    DefinitionRevision,
    DomainIssueDTO,
    FieldKey,
    HistoryCleanupPreviewDTO,
    HistoryCleanupPreviewResult,
    OwnerChangedDTO,
    OwnerMutationResult,
    OwnerNoOpDTO,
    OwnerRefDTO,
    PreviewToken,
    ResourceRevision,
    SpecificationGraphLoadRequest,
    SpecificationProjectionRequest,
    SpecificationResolutionRequest,
    StoredSpecificationEntryDTO,
)
from assets.services.specifications.loader import load_specification_graph
from assets.services.specifications.locking import catalogue_transaction_lock
from assets.services.specifications.preview_tokens import (
    OwnerRef as PreviewOwnerRef,
)
from assets.services.specifications.preview_tokens import (
    PreviewTokenError,
    PreviewTokenExpectation,
    issue_preview_token,
    normalized_input_digest,
    verify_preview_token,
)
from core.context import override_current_tenant_scope
from extras.services.specifications.composition import (
    SpecificationDefinitionError,
    resolve_specification_definition,
)
from extras.services.specifications.projection import project_specification_values
from organization.services.access_scope import (
    AccessScopeResolvedDTO,
    ActorContextDTO,
    ResolvedAccessAuthorizationDTO,
    reauthorize_access_scope,
)
from organization.services.catalogue_authorization import has_provider_catalogue_permission

from ._command_support import (
    issue,
    json_values_equal,
    map_structure_error,
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
from ._create_commands import _preview_token_key
from ._history_support import history_state_digest, is_history_cleanup_eligible, normalize_history_keys

_DEFAULT_DB = DEFAULT_DB_ALIAS
_ASSET_TYPE_CHANGE_PERMISSION = "change_assettype"
_ASSET_CHANGE_PERMISSION = "assets.change_asset"
_HISTORY_OPERATION = "cleanup_asset_specification_history"
_TYPE_COMMAND_KIND = "cleanup_asset_type_history"
_ASSET_COMMAND_KIND = "cleanup_asset_history"


def _normalized_keys(keys: tuple[FieldKey, ...]) -> tuple[FieldKey, ...]:
    return tuple(FieldKey(key) for key in normalize_history_keys(keys))


def _history_input_digest(keys: tuple[FieldKey, ...]) -> str:
    return normalized_input_digest({"keys": tuple(str(key) for key in keys)})


def _history_issue(code: str, key: FieldKey) -> DomainIssueDTO:
    return issue(code, path=("keys", str(key)), field_key=key)


def _validate_preview_type_inputs(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> tuple[int, tuple[FieldKey, ...]]:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")
    asset_type_id = positive_id(asset_type_id, "Asset Type ID")
    normalized_keys = _normalized_keys(keys)
    revision_string(expected_resource_revision, "expected_resource_revision")
    revision_string(expected_definition_revision, "expected_definition_revision")
    return asset_type_id, normalized_keys


def _validate_write_type_inputs(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    keys: tuple[FieldKey, ...],
    preview_token: PreviewToken,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> tuple[int, tuple[FieldKey, ...]]:
    asset_type_id, normalized_keys = _validate_preview_type_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    if type(preview_token) is not str:
        raise TypeError("preview_token must be a string")
    return asset_type_id, normalized_keys


def _validate_preview_asset_inputs(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> tuple[int, tuple[FieldKey, ...]]:
    if not isinstance(authorization, ResolvedAccessAuthorizationDTO):
        raise TypeError("authorization must be a ResolvedAccessAuthorizationDTO")
    asset_id = positive_id(asset_id, "Asset ID")
    normalized_keys = _normalized_keys(keys)
    revision_string(expected_resource_revision, "expected_resource_revision")
    revision_string(expected_definition_revision, "expected_definition_revision")
    return asset_id, normalized_keys


def _validate_write_asset_inputs(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    keys: tuple[FieldKey, ...],
    preview_token: PreviewToken,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> tuple[int, tuple[FieldKey, ...]]:
    asset_id, normalized_keys = _validate_preview_asset_inputs(
        authorization=authorization,
        asset_id=asset_id,
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    if type(preview_token) is not str:
        raise TypeError("preview_token must be a string")
    return asset_id, normalized_keys


def _load_history_plan(
    *,
    owner: Asset | AssetType,
    owner_ref: OwnerRefDTO,
    target_kind: str,
    keys: tuple[FieldKey, ...],
) -> tuple[dict[str, object], object, tuple[DomainIssueDTO, ...], str] | CommandRejectedDTO:
    try:
        stored_values = stored_values_for(owner)
        asset_type_id = owner.pk if target_kind == "asset_type" else owner.asset_type_id
        type_ids = () if asset_type_id is None else (positive_id(asset_type_id, "Asset Type ID"),)
        graph = load_specification_graph(
            SpecificationGraphLoadRequest(
                asset_type_ids=tuple(type_ids),
                requested_target_kinds=frozenset({target_kind}),  # type: ignore[arg-type]
                requested_field_keys=frozenset(keys),
            )
        )
        memberships = graph.type_memberships.get(asset_type_id, ()) if asset_type_id is not None else ()
        definition = resolve_specification_definition(
            # This DTO is deliberately built here rather than reusing the edit
            # helper: history needs the graph's historical index as well.
            SpecificationResolutionRequest(
                ordered_memberships=memberships,
                loaded_graph=graph,
                target_kind=target_kind,
            )
        )
        entries = tuple(
            StoredSpecificationEntryDTO(key=key, value=stored_values[key]) for key in keys if key in stored_values
        )
        projection = project_specification_values(
            SpecificationProjectionRequest(
                definition=definition,
                stored_entries=entries,
                historical_definitions_by_key=graph.historical_definitions_by_key,
            )
        )
        by_key = {entry.key: entry for entry in projection.entries}
        issues: list[DomainIssueDTO] = []
        for key in keys:
            if key not in stored_values or key not in by_key:
                issues.append(_history_issue("UNKNOWN_FIELD_KEY", key))
                continue
            if not is_history_cleanup_eligible(by_key[key]):
                issues.append(_history_issue("READ_ONLY_FIELD", key))
        digest = history_state_digest(keys, stored_values)
        return stored_values, definition, tuple(issues), digest
    except (SpecificationDefinitionError, TypeError, ValueError, KeyError):
        return map_structure_error(owner_ref)


def _type_actor_or_unavailable(actor: ActorContextDTO):
    actor_model = reload_actor(actor)
    if actor_model is None or not has_provider_catalogue_permission(
        actor_model,
        f"{AssetType._meta.app_label}.{_ASSET_TYPE_CHANGE_PERMISSION}",
    ):
        return None
    return actor_model


def _asset_actor_or_rejection(
    authorization: ResolvedAccessAuthorizationDTO,
    owner: Asset,
    owner_ref: OwnerRefDTO,
) -> object | CommandRejectedDTO:
    fresh_scope = reauthorize_access_scope(authorization)
    if not isinstance(fresh_scope, AccessScopeResolvedDTO):
        return unavailable()
    if (
        authorization.request.operation != _HISTORY_OPERATION
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


def _type_expectation(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: str,
    expected_definition_revision: str,
    historical_state_digest: str,
) -> PreviewTokenExpectation:
    return PreviewTokenExpectation(
        actor_id=actor.actor_id,
        authentication_revision=actor.authentication_revision,
        access_scope_fingerprint=None,
        command_kind=_TYPE_COMMAND_KIND,
        target=PreviewOwnerRef("asset_type", asset_type_id),
        normalized_input_digest=_history_input_digest(keys),
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=None,
        historical_state_digest=historical_state_digest,
    )


def _asset_expectation(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: int,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: str,
    expected_definition_revision: str,
    historical_state_digest: str,
) -> PreviewTokenExpectation:
    return PreviewTokenExpectation(
        actor_id=authorization.actor.actor_id,
        authentication_revision=authorization.actor.authentication_revision,
        access_scope_fingerprint=authorization.initial_scope.access_scope_fingerprint,
        command_kind=_ASSET_COMMAND_KIND,
        target=PreviewOwnerRef("asset", asset_id),
        normalized_input_digest=_history_input_digest(keys),
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=None,
        historical_state_digest=historical_state_digest,
    )


def _verify_token_or_rejection(
    *,
    token: str,
    expected: PreviewTokenExpectation,
    owner_ref: OwnerRefDTO,
) -> CommandRejectedDTO | None:
    try:
        verify_preview_token(token, expected=expected, key=_preview_token_key())
    except PreviewTokenError:
        return rejected(owner_ref, stale_plan_issue())
    return None


def _preview_type_locked(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: str,
    expected_definition_revision: str,
) -> HistoryCleanupPreviewResult:
    owner_ref = OwnerRefDTO("asset_type", asset_type_id)
    owner = (
        AssetType.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=asset_type_id, deleted_at__isnull=True)
        .first()
    )
    if owner is None:
        return unavailable()
    if _type_actor_or_unavailable(actor) is None:
        return unavailable()

    plan = _load_history_plan(owner=owner, owner_ref=owner_ref, target_kind="asset_type", keys=keys)
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, definition, issues, digest = plan
    actual_resource_revision = resource_revision_for_owner(owner)
    actual_definition_revision = DefinitionRevision(definition.revision)
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=actual_definition_revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)
    token = issue_preview_token(
        _type_expectation(
            actor=actor,
            asset_type_id=asset_type_id,
            keys=keys,
            expected_resource_revision=actual_resource_revision,
            expected_definition_revision=actual_definition_revision,
            historical_state_digest=digest,
        ),
        key=_preview_token_key(),
    )
    del stored_values
    return HistoryCleanupPreviewDTO(
        preview_token=PreviewToken(token),
        owner=owner_ref,
        keys=keys,
        expected_resource_revision=actual_resource_revision,
        expected_definition_revision=actual_definition_revision,
        historical_state_digest=digest,
        issues=issues,
    )


def _save_history_owner(
    owner: Asset | AssetType,
    actor: object,
    *,
    update_fields: tuple[str, ...],
    custom_field_data: dict[str, object] | None = None,
) -> None:
    """Run the normal audited save with the locked Asset's tenant scope bound."""
    if isinstance(owner, Asset):
        with override_current_tenant_scope(owner.tenant):
            save_owner_in_savepoint(
                owner,
                actor,
                using=_DEFAULT_DB,
                update_fields=update_fields,
                custom_field_data=custom_field_data,
            )
        return
    save_owner_in_savepoint(
        owner,
        actor,
        using=_DEFAULT_DB,
        update_fields=update_fields,
        custom_field_data=custom_field_data,
    )


def _history_state_or_rejection(
    owner: Asset | AssetType,
    owner_ref: OwnerRefDTO,
    keys: tuple[FieldKey, ...],
) -> tuple[dict[str, object], str] | CommandRejectedDTO:
    try:
        stored_values = stored_values_for(owner)
        return stored_values, history_state_digest(keys, stored_values)
    except (TypeError, ValueError):
        return map_structure_error(owner_ref)


def _cleanup_plan_or_rejection(
    *,
    owner: Asset | AssetType,
    owner_ref: OwnerRefDTO,
    target_kind: str,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: str,
    expected_definition_revision: str,
) -> tuple[dict[str, object], ResourceRevision, DefinitionRevision] | CommandRejectedDTO:
    plan = _load_history_plan(owner=owner, owner_ref=owner_ref, target_kind=target_kind, keys=keys)
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, definition, issues, _digest = plan
    actual_resource_revision = resource_revision_for_owner(owner)
    actual_definition_revision = DefinitionRevision(definition.revision)
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=actual_definition_revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)
    if issues:
        return rejected(owner_ref, *issues)
    return stored_values, actual_resource_revision, actual_definition_revision


def _apply_history_cleanup(
    *,
    owner: Asset | AssetType,
    actor: object,
    owner_ref: OwnerRefDTO,
    keys: tuple[FieldKey, ...],
    stored_values: dict[str, object],
    resource_revision: ResourceRevision,
    definition_revision: DefinitionRevision,
) -> OwnerMutationResult:
    if not keys:
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=resource_revision,
            definition_revision=definition_revision,
        )
    proposed_values = dict(stored_values)
    for key in keys:
        proposed_values.pop(key, None)
    if json_values_equal(owner.custom_field_data, proposed_values):
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=resource_revision,
            definition_revision=definition_revision,
        )
    try:
        _save_history_owner(
            owner,
            actor,
            update_fields=("custom_field_data", "updated_at"),
            custom_field_data=proposed_values,
        )
    except ValidationError:
        return rejected(owner_ref, issue("REFERENCE_CONFLICT"))
    return OwnerChangedDTO(
        outcome="changed",
        owner=owner_ref,
        resource_revision=resource_revision_for_owner(owner),
        definition_revision=definition_revision,
    )


def _cleanup_type_locked(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
    keys: tuple[FieldKey, ...],
    preview_token: str,
    expected_resource_revision: str,
    expected_definition_revision: str,
) -> OwnerMutationResult:
    owner_ref = OwnerRefDTO("asset_type", asset_type_id)
    owner = (
        AssetType.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=asset_type_id, deleted_at__isnull=True)
        .first()
    )
    if owner is None:
        return unavailable()
    actor_model = _type_actor_or_unavailable(actor)
    if actor_model is None:
        return unavailable()

    state = _history_state_or_rejection(owner, owner_ref, keys)
    if isinstance(state, CommandRejectedDTO):
        return state
    _stored_values, digest = state
    token_rejection = _verify_token_or_rejection(
        token=preview_token,
        expected=_type_expectation(
            actor=actor,
            asset_type_id=asset_type_id,
            keys=keys,
            expected_resource_revision=expected_resource_revision,
            expected_definition_revision=expected_definition_revision,
            historical_state_digest=digest,
        ),
        owner_ref=owner_ref,
    )
    if token_rejection is not None:
        return token_rejection

    plan = _cleanup_plan_or_rejection(
        owner=owner,
        owner_ref=owner_ref,
        target_kind="asset_type",
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, actual_resource_revision, actual_definition_revision = plan
    return _apply_history_cleanup(
        owner=owner,
        actor=actor_model,
        owner_ref=owner_ref,
        keys=keys,
        stored_values=stored_values,
        resource_revision=actual_resource_revision,
        definition_revision=actual_definition_revision,
    )


def _preview_asset_locked(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: int,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: str,
    expected_definition_revision: str,
) -> HistoryCleanupPreviewResult:
    owner_ref = OwnerRefDTO("asset", asset_id)
    owner = Asset.objects.using(_DEFAULT_DB).select_for_update().filter(pk=asset_id, deleted_at__isnull=True).first()
    if owner is None:
        return unavailable()
    actor_model = _asset_actor_or_rejection(authorization, owner, owner_ref)
    if isinstance(actor_model, CommandRejectedDTO):
        return actor_model

    plan = _load_history_plan(owner=owner, owner_ref=owner_ref, target_kind="asset", keys=keys)
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, definition, issues, digest = plan
    actual_resource_revision = resource_revision_for_owner(owner)
    actual_definition_revision = DefinitionRevision(definition.revision)
    revision_issues = stale_revision_issues(
        expected_resource_revision=expected_resource_revision,
        actual_resource_revision=actual_resource_revision,
        expected_definition_revision=expected_definition_revision,
        actual_definition_revision=actual_definition_revision,
    )
    if revision_issues:
        return rejected(owner_ref, *revision_issues)
    token = issue_preview_token(
        _asset_expectation(
            authorization=authorization,
            asset_id=asset_id,
            keys=keys,
            expected_resource_revision=actual_resource_revision,
            expected_definition_revision=actual_definition_revision,
            historical_state_digest=digest,
        ),
        key=_preview_token_key(),
    )
    del stored_values, actor_model
    return HistoryCleanupPreviewDTO(
        preview_token=PreviewToken(token),
        owner=owner_ref,
        keys=keys,
        expected_resource_revision=actual_resource_revision,
        expected_definition_revision=actual_definition_revision,
        historical_state_digest=digest,
        issues=issues,
    )


def _cleanup_asset_locked(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: int,
    keys: tuple[FieldKey, ...],
    preview_token: str,
    expected_resource_revision: str,
    expected_definition_revision: str,
) -> OwnerMutationResult:
    owner_ref = OwnerRefDTO("asset", asset_id)
    owner = Asset.objects.using(_DEFAULT_DB).select_for_update().filter(pk=asset_id, deleted_at__isnull=True).first()
    if owner is None:
        return unavailable()
    actor_model = _asset_actor_or_rejection(authorization, owner, owner_ref)
    if isinstance(actor_model, CommandRejectedDTO):
        return actor_model

    state = _history_state_or_rejection(owner, owner_ref, keys)
    if isinstance(state, CommandRejectedDTO):
        return state
    _stored_values, digest = state
    token_rejection = _verify_token_or_rejection(
        token=preview_token,
        expected=_asset_expectation(
            authorization=authorization,
            asset_id=asset_id,
            keys=keys,
            expected_resource_revision=expected_resource_revision,
            expected_definition_revision=expected_definition_revision,
            historical_state_digest=digest,
        ),
        owner_ref=owner_ref,
    )
    if token_rejection is not None:
        return token_rejection

    plan = _cleanup_plan_or_rejection(
        owner=owner,
        owner_ref=owner_ref,
        target_kind="asset",
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    stored_values, actual_resource_revision, actual_definition_revision = plan
    return _apply_history_cleanup(
        owner=owner,
        actor=actor_model,
        owner_ref=owner_ref,
        keys=keys,
        stored_values=stored_values,
        resource_revision=actual_resource_revision,
        definition_revision=actual_definition_revision,
    )


def preview_asset_type_history_cleanup(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> HistoryCleanupPreviewResult:
    asset_type_id, normalized_keys = _validate_preview_type_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _preview_type_locked(
                actor=actor,
                asset_type_id=asset_type_id,
                keys=normalized_keys,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
            )


def cleanup_asset_type_history(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    keys: tuple[FieldKey, ...],
    preview_token: PreviewToken,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> OwnerMutationResult:
    asset_type_id, normalized_keys = _validate_write_type_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        keys=keys,
        preview_token=preview_token,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _cleanup_type_locked(
                actor=actor,
                asset_type_id=asset_type_id,
                keys=normalized_keys,
                preview_token=preview_token,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
            )


def preview_asset_history_cleanup(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    keys: tuple[FieldKey, ...],
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> HistoryCleanupPreviewResult:
    asset_id, normalized_keys = _validate_preview_asset_inputs(
        authorization=authorization,
        asset_id=asset_id,
        keys=keys,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _preview_asset_locked(
                authorization=authorization,
                asset_id=asset_id,
                keys=normalized_keys,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
            )


def cleanup_asset_history(
    *,
    authorization: ResolvedAccessAuthorizationDTO,
    asset_id: AssetId,
    keys: tuple[FieldKey, ...],
    preview_token: PreviewToken,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
) -> OwnerMutationResult:
    asset_id, normalized_keys = _validate_write_asset_inputs(
        authorization=authorization,
        asset_id=asset_id,
        keys=keys,
        preview_token=preview_token,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _cleanup_asset_locked(
                authorization=authorization,
                asset_id=asset_id,
                keys=normalized_keys,
                preview_token=preview_token,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
            )


__all__ = [
    "cleanup_asset_history",
    "cleanup_asset_type_history",
    "preview_asset_history_cleanup",
    "preview_asset_type_history_cleanup",
]
