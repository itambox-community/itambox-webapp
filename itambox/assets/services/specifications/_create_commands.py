"""Locked Type-create, create-preview, and Category-default apply commands.

This module owns the B2 write path for creating Asset Types and for applying
Category default compositions to existing Asset Types.  It reuses the
catalogue transaction lock, the prospective loader, the pure codec, and the
signed preview-token kernel from the sibling modules; it adds only the
Category-default snapshot recompute, the caller-owned signing-key wiring, and
the bounded staged-image consumption the accepted contract requires.

Error precedence follows the accepted T01 contract: syntactic missing
preconditions, then authentication/authorization and inaccessible Category or
Manufacturer (nondisclosing ``OBJECT_UNAVAILABLE``), then token claims
(``STALE_PLAN``), then Category-default snapshot and owner/definition
revisions (``STALE_RESOURCE``/``STALE_DEFINITION``), and only then ordinary
domain validation (native field parity, references, graph structure, codec
requiredness).  Structural graph failures never precede authority or token
diagnostics, and a usable graph revision is compared before lifecycle or
applicability acceptance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, models, transaction

from assets.models.catalog import (
    AssetRole,
    AssetType,
    AssetTypeFieldset,
    Category,
    CategoryDefaultFieldset,
    Depreciation,
    Manufacturer,
)
from assets.services.specifications.contracts import (
    AssetTypeCreateResult,
    AssetTypeId,
    AssetTypeNativeCreateInputDTO,
    AssetTypePreviewDTO,
    AssetTypePreviewResult,
    CategoryDefaultSnapshotDTO,
    CategoryDefaultSnapshotRevision,
    CommandRejectedDTO,
    DefinitionRevision,
    DomainIssueDTO,
    FieldsetSelectionDTO,
    OrderedFieldsetMembershipDTO,
    OwnerChangedDTO,
    OwnerCreatedDTO,
    OwnerMutationResult,
    OwnerNoOpDTO,
    OwnerRefDTO,
    PreviewToken,
    ResourceRevision,
    SpecificationPatchDTO,
)
from assets.services.specifications.loader import (
    fieldset_ids_for_identities,
    load_prospective_specification_graph,
)
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
from extras.models import CustomFieldset, Tag
from extras.services.specifications.composition import SpecificationDefinitionError
from extras.services.specifications.contracts import QualifiedIdentity
from organization.services.access_scope import ActorContextDTO
from organization.services.catalogue_authorization import has_provider_catalogue_permission

from ._command_support import (
    actor_change_context,
    issue,
    json_values_equal,
    load_prospective_definition,
    map_structure_error,
    normalize_patch,
    positive_id,
    rejected,
    reload_actor,
    resource_revision_for_owner,
    revision_string,
    stale_plan_issue,
    stored_values_for,
    unavailable,
)
from ._composition_commands import (
    _current_membership_rows,
    _persist_replacement,
    _reference_rejection,
    _validate_proposed_graph,
)
from ._image_staging import (
    CREATE_COMMAND_KIND,
    consume_stage,
    lock_stage_for_consume,
    preview_stage_or_none,
)

_DEFAULT_DB = DEFAULT_DB_ALIAS
_ADD_PERMISSION = "add_assettype"
_CHANGE_PERMISSION = "change_assettype"
_TARGET_KIND = "asset_type"
_CREATE_COMMAND_KIND = CREATE_COMMAND_KIND
_APPLY_COMMAND_KIND = "apply_category_defaults"
_SNAPSHOT_VERSION = 1
_REFERENCE_CONFLICT_MESSAGE = "specifications.reference_conflict"
_STALE_RESOURCE_MESSAGE = "specifications.stale_resource"
_STALE_DEFINITION_MESSAGE = "specifications.stale_definition"
_INVALID_RANGE_MESSAGE = "specifications.invalid_range"
_INVALID_TYPE_MESSAGE = "specifications.invalid_type"


@dataclass
class _CreateDependencyLocks:
    """Locked reference rows gathered in deterministic model-label/PK order.

    Category and Manufacturer unavailability is already mapped to the
    nondisclosing ``OBJECT_UNAVAILABLE`` result during locking; the remaining
    flags feed the row-9 reference validation so an invalid token or stale
    revision is always reported first.
    """

    role_exists: bool = True
    depreciation_exists: bool = True
    tags_ok: bool = True
    stage: Any = None


def _preview_token_key() -> str:
    """Return the caller-owned preview signing key.

    The preview-token kernel deliberately refuses a settings fallback; the
    commands wire ordinary server settings here.  The same derivation is used
    by both the preview and the write command, so a command always verifies
    the tokens it issued.
    """
    key = getattr(settings, "SECRET_KEY", None)
    if not isinstance(key, str) or not key:
        raise RuntimeError("a non-empty SECRET_KEY is required to sign specification preview tokens")
    return key


def _patch_payload(patch: SpecificationPatchDTO) -> dict[str, object]:
    """Canonical patch JSON without deep-copying the frozen mapping."""
    return {"set_values": dict(patch.set_values), "clear_keys": patch.clear_keys}


def _create_input_digest(
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
) -> str:
    """Bind the complete canonical native/selection/patch create input."""
    return normalized_input_digest(
        {
            "native": asdict(native),
            "fieldsets": asdict(fieldsets),
            "patch": _patch_payload(patch),
        }
    )


def _apply_input_digest(patch: SpecificationPatchDTO) -> str:
    """Bind the canonical apply input; the target is a separate claim."""
    return normalized_input_digest({"patch": _patch_payload(patch)})


def _consumes_category_defaults(
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
) -> bool:
    return native.category_id is not None and fieldsets.presence == "omitted"


def _missing_create_preconditions(
    *,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    preview_token: str | None,
    expected_definition_revision: str | None,
    expected_category_default_snapshot_revision: str | None,
) -> tuple[DomainIssueDTO, ...]:
    missing: list[DomainIssueDTO] = []
    if expected_definition_revision is None:
        missing.append(issue("MISSING_PRECONDITION", path=("expected_definition_revision",)))
    if not _consumes_category_defaults(native, fieldsets):
        return tuple(missing)
    if preview_token is None:
        missing.append(issue("MISSING_PRECONDITION", path=("preview_token",)))
    if expected_category_default_snapshot_revision is None:
        missing.append(issue("MISSING_PRECONDITION", path=("expected_category_default_snapshot_revision",)))
    return tuple(missing)


def _missing_apply_preconditions(
    *,
    preview_token: str | None,
    expected_resource_revision: str | None,
    expected_definition_revision: str | None,
    expected_category_default_snapshot_revision: str | None,
) -> tuple[DomainIssueDTO, ...]:
    missing: list[DomainIssueDTO] = []
    if preview_token is None:
        missing.append(issue("MISSING_PRECONDITION", path=("preview_token",)))
    if expected_resource_revision is None:
        missing.append(issue("MISSING_PRECONDITION", path=("expected_resource_revision",)))
    if expected_definition_revision is None:
        missing.append(issue("MISSING_PRECONDITION", path=("expected_definition_revision",)))
    if expected_category_default_snapshot_revision is None:
        missing.append(issue("MISSING_PRECONDITION", path=("expected_category_default_snapshot_revision",)))
    return tuple(missing)


def _validate_create_preview_inputs(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
) -> None:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")
    if not isinstance(native, AssetTypeNativeCreateInputDTO):
        raise TypeError("native must be an AssetTypeNativeCreateInputDTO")
    if not isinstance(fieldsets, FieldsetSelectionDTO):
        raise TypeError("fieldsets must be a FieldsetSelectionDTO")
    if not isinstance(patch, SpecificationPatchDTO):
        raise TypeError("patch must be a SpecificationPatchDTO")


def _validate_create_inputs(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
    preview_token: str | None,
    expected_definition_revision: str | None,
    expected_category_default_snapshot_revision: str | None,
) -> None:
    _validate_create_preview_inputs(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
        patch=patch,
    )
    if preview_token is not None and type(preview_token) is not str:
        raise TypeError("preview_token must be a string or None")
    if expected_definition_revision is not None:
        revision_string(expected_definition_revision, "expected_definition_revision")
    if expected_category_default_snapshot_revision is not None:
        revision_string(
            expected_category_default_snapshot_revision,
            "expected_category_default_snapshot_revision",
        )


def _validate_apply_shared_inputs(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    patch: SpecificationPatchDTO,
) -> None:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")
    if not isinstance(patch, SpecificationPatchDTO):
        raise TypeError("patch must be a SpecificationPatchDTO")
    positive_id(asset_type_id, "Asset Type ID")


def _reloaded_authorized_actor(
    actor: ActorContextDTO,
    model: type[models.Model],
    codename: str,
):
    """Reload the active actor and require provider-scoped catalogue authority."""
    actor_model = reload_actor(actor)
    permission = f"{model._meta.app_label}.{codename}"
    if actor_model is None or not has_provider_catalogue_permission(actor_model, permission):
        return None
    return actor_model


def _available_manufacturer(manufacturer_id: int):
    return Manufacturer.objects.using(_DEFAULT_DB).filter(pk=manufacturer_id, deleted_at__isnull=True).first()


def _available_category(category_id: int):
    return Category.objects.using(_DEFAULT_DB).filter(pk=category_id, deleted_at__isnull=True).first()


def _tags_exist(tag_ids: tuple[int, ...], *, using: str = _DEFAULT_DB) -> bool:
    if not tag_ids:
        return True
    return Tag.objects.using(using).filter(pk__in=tag_ids, deleted_at__isnull=True).count() == len(tag_ids)


def _lock_asset_type(asset_type_id: int):
    return (
        AssetType.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=asset_type_id, deleted_at__isnull=True)
        .first()
    )


def _lock_category(category_id: int):
    return (
        Category.objects.using(_DEFAULT_DB).select_for_update().filter(pk=category_id, deleted_at__isnull=True).first()
    )


def _lock_create_references(
    native: AssetTypeNativeCreateInputDTO,
    actor: ActorContextDTO,
    *,
    using: str = _DEFAULT_DB,
) -> _CreateDependencyLocks | CommandRejectedDTO:
    """Lock every referenced row in deterministic model-label/PK order.

    Order: AssetRole, staged-image row, Category, Depreciation, Manufacturer,
    then Tag rows by ascending PK.  Category and Manufacturer disappearances
    are the nondisclosing ``OBJECT_UNAVAILABLE`` availability check; the
    remaining references are locked here and validated only after the
    authority/token/revision gates (row 9).
    """
    locks = _CreateDependencyLocks()
    if not isinstance(native, AssetTypeNativeCreateInputDTO):
        raise TypeError("native must be an AssetTypeNativeCreateInputDTO")
    if native.suggested_asset_role_id is not None:
        locks.role_exists = (
            AssetRole.objects.using(using)
            .select_for_update()
            .filter(pk=native.suggested_asset_role_id, deleted_at__isnull=True)
            .exists()
        )
    if native.staged_image_id is not None:
        locks.stage = lock_stage_for_consume(
            native.staged_image_id,
            actor,
            CREATE_COMMAND_KIND,
            using=using,
        )
    if native.category_id is not None:
        if _lock_category(native.category_id) is None:
            return unavailable()
    if native.depreciation_id is not None:
        locks.depreciation_exists = (
            Depreciation.objects.using(using)
            .select_for_update()
            .filter(pk=native.depreciation_id, deleted_at__isnull=True)
            .exists()
        )
    if _lock_manufacturer(native.manufacturer_id) is None:
        return unavailable()
    if native.tag_ids:
        locked_tags = list(
            Tag.objects.using(using)
            .select_for_update()
            .filter(pk__in=native.tag_ids, deleted_at__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        locks.tags_ok = len(locked_tags) == len(native.tag_ids)
    return locks


def _lock_manufacturer(manufacturer_id: int):
    return (
        Manufacturer.objects.using(_DEFAULT_DB)
        .select_for_update()
        .filter(pk=manufacturer_id, deleted_at__isnull=True)
        .first()
    )


def _category_default_identities_read(category_id: int, *, using: str = _DEFAULT_DB) -> tuple[str, ...]:
    """Raw structural read of a Category's default Fieldset identities.

    This is not the authoritative snapshot: unresolvable Fieldsets are skipped
    here and surface as the structure error from the locked recomputation,
    after the authority and token gates.
    """
    rows = list(
        CategoryDefaultFieldset.objects.using(using)
        .filter(category_id=category_id)
        .order_by("position", "fieldset_id", "pk")
    )
    fieldset_rows = {
        row["pk"]: row
        for row in CustomFieldset.objects.using(using)
        .filter(pk__in=[row.fieldset_id for row in rows])
        .values("pk", "namespace", "slug")
    }
    identities: list[str] = []
    for row in rows:
        fieldset_row = fieldset_rows.get(row.fieldset_id)
        if fieldset_row is None:
            continue
        identities.append(f"{fieldset_row['namespace']}/{fieldset_row['slug']}")
    return tuple(identities)


def _proposed_identities_read(
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    *,
    using: str = _DEFAULT_DB,
) -> tuple[str, ...]:
    if fieldsets.presence == "explicit":
        return tuple(str(identity) for identity in fieldsets.identities)
    if native.category_id is None:
        return ()
    return _category_default_identities_read(native.category_id, using=using)


def _proposed_identities(
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    snapshot: CategoryDefaultSnapshotDTO | None,
) -> tuple[str, ...]:
    if fieldsets.presence == "explicit":
        return tuple(str(identity) for identity in fieldsets.identities)
    if snapshot is None:
        return ()
    return tuple(str(membership.fieldset_identity) for membership in snapshot.memberships)


def _recompute_category_default_snapshot(
    category_id: int,
    *,
    using: str = _DEFAULT_DB,
) -> CategoryDefaultSnapshotDTO:
    """Recompute the distinct Category-default snapshot revision under the lock.

    The canonical source state contains the Category identity, the complete
    ordered default-membership list with every stored ordinal and Fieldset
    identity, and each referenced Fieldset's current resource revision and
    lifecycle.  Fieldset revisions come from the loader so the snapshot cannot
    drift from the definition codec's source of truth.
    """
    rows = list(
        CategoryDefaultFieldset.objects.using(using)
        .filter(category_id=category_id)
        .order_by("position", "fieldset_id", "pk")
    )
    fieldset_rows = {
        row["pk"]: row
        for row in CustomFieldset.objects.using(using)
        .filter(pk__in=[row.fieldset_id for row in rows])
        .values("pk", "namespace", "slug")
    }
    identities: list[str] = []
    for row in rows:
        fieldset_row = fieldset_rows.get(row.fieldset_id)
        if fieldset_row is None:
            raise ValueError(f"unresolved default Fieldset: {row.fieldset_id}")
        identities.append(f"{fieldset_row['namespace']}/{fieldset_row['slug']}")

    graph = load_prospective_specification_graph(
        fieldset_identities=tuple(QualifiedIdentity(identity) for identity in identities),
        requested_target_kinds=frozenset({_TARGET_KIND}),
        requested_field_keys=frozenset(),
    )
    memberships: list[dict[str, object]] = []
    membership_dtos: list[OrderedFieldsetMembershipDTO] = []
    for row, identity in zip(rows, identities, strict=True):
        fieldset_dto = graph.fieldsets_by_identity.get(QualifiedIdentity(identity))
        if fieldset_dto is None:
            raise ValueError(f"unresolved default Fieldset graph: {identity}")
        memberships.append(
            {
                "ordinal": row.position,
                "fieldset_identity": identity,
                "fieldset_resource_revision": str(fieldset_dto.resource_revision),
                "fieldset_lifecycle": fieldset_dto.lifecycle,
            }
        )
        membership_dtos.append(
            OrderedFieldsetMembershipDTO(
                fieldset_identity=QualifiedIdentity(identity),
                ordinal=row.position,
            )
        )
    snapshot_payload = {
        "version": _SNAPSHOT_VERSION,
        "category_id": category_id,
        "memberships": memberships,
    }
    serialized = json.dumps(
        snapshot_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CategoryDefaultSnapshotDTO(
        category_id=category_id,
        revision=CategoryDefaultSnapshotRevision("sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()),
        memberships=tuple(membership_dtos),
    )


def _snapshot_or_rejection(
    category_id: int,
    *,
    owner_ref: OwnerRefDTO | None,
):
    """Recompute the Category-default snapshot or map structural failures."""
    try:
        return _recompute_category_default_snapshot(category_id)
    except ValueError:
        return map_structure_error(owner_ref)


def _prospective_definition_or_rejection(
    identities: Sequence[str],
    *,
    stored_values: dict[str, object],
    owner_ref: OwnerRefDTO | None,
    expected_definition_revision: str | None = None,
):
    """Load the prospective definition, comparing revisions before graph checks.

    Where a usable graph/revision exists, an expected-definition mismatch is
    reported as ``STALE_DEFINITION`` before any lifecycle or applicability
    acceptance, so an explicit selection whose member was deprecated since the
    preview is stale, not a reference conflict.  Truly unresolvable graphs
    (missing Fieldsets) are reference conflicts; they never precede the
    authority or token gates because callers only reach this helper after them.
    """
    try:
        definition, definitions, graph = load_prospective_definition(
            identities,
            _TARGET_KIND,
            tuple(stored_values),
        )
    except (SpecificationDefinitionError, ValueError, TypeError):
        return _reference_rejection(owner_ref)
    if expected_definition_revision is not None and definition.revision != expected_definition_revision:
        return rejected(
            owner_ref,
            issue(
                "STALE_DEFINITION",
                path=("expected_definition_revision",),
                message_key=_STALE_DEFINITION_MESSAGE,
            ),
        )
    graph_rejection = _validate_proposed_graph(
        graph=graph,
        identities=identities,
        owner_ref=owner_ref,
    )
    if graph_rejection is not None:
        return graph_rejection
    return definition, definitions, graph


def _native_field_issues(native: AssetTypeNativeCreateInputDTO) -> tuple[DomainIssueDTO, ...]:
    """Native scalar parity against the real AssetType field limits/validators."""
    issues: list[DomainIssueDTO] = []
    for field_name in ("model", "slug", "part_number", "ean", "region", "configuration"):
        value = getattr(native, field_name)
        if value is None:
            continue
        if len(value) > AssetType._meta.get_field(field_name).max_length:
            issues.append(issue("INVALID_RANGE", path=(field_name,), message_key=_INVALID_RANGE_MESSAGE))
    if native.slug is not None:
        try:
            for validator in AssetType._meta.get_field("slug").validators:
                validator(native.slug)
        except ValidationError:
            issues.append(issue("INVALID_TYPE", path=("slug",), message_key=_INVALID_TYPE_MESSAGE))
    if native.eol_months is not None and native.eol_months < 0:
        issues.append(issue("INVALID_RANGE", path=("eol_months",), message_key=_INVALID_RANGE_MESSAGE))
    return tuple(issues)


def _explicit_slug_conflict_issue(native: AssetTypeNativeCreateInputDTO, *, using: str = _DEFAULT_DB):
    """Current duplicate explicit-slug conflict in the shared preview/write plan."""
    if native.slug is None:
        return None
    if AssetType.objects.using(using).filter(slug=native.slug, deleted_at__isnull=True).exists():
        return issue("REFERENCE_CONFLICT", path=("slug",), message_key=_REFERENCE_CONFLICT_MESSAGE)
    return None


def _reference_issue(path: str) -> DomainIssueDTO:
    return issue("REFERENCE_CONFLICT", path=(path,), message_key=_REFERENCE_CONFLICT_MESSAGE)


def _locked_reference_issues(native: AssetTypeNativeCreateInputDTO, locks: _CreateDependencyLocks):
    """Row-9 reference availability from the already-locked rows."""
    issues: list[DomainIssueDTO] = []
    if native.suggested_asset_role_id is not None and not locks.role_exists:
        issues.append(_reference_issue("suggested_asset_role_id"))
    if native.depreciation_id is not None and not locks.depreciation_exists:
        issues.append(_reference_issue("depreciation_id"))
    if native.tag_ids and not locks.tags_ok:
        issues.append(_reference_issue("tag_ids"))
    if native.staged_image_id is not None and locks.stage is None:
        issues.append(_reference_issue("staged_image_id"))
    return tuple(issues)


def _preview_reference_issues(
    native: AssetTypeNativeCreateInputDTO,
    actor: ActorContextDTO,
    *,
    using: str = _DEFAULT_DB,
    now=None,
) -> tuple[DomainIssueDTO, ...]:
    """Read-only reference availability for previews (never consumes locks)."""
    issues: list[DomainIssueDTO] = []
    if native.suggested_asset_role_id is not None:
        if (
            not AssetRole.objects.using(using)
            .filter(pk=native.suggested_asset_role_id, deleted_at__isnull=True)
            .exists()
        ):
            issues.append(_reference_issue("suggested_asset_role_id"))
    if native.depreciation_id is not None:
        if not Depreciation.objects.using(using).filter(pk=native.depreciation_id, deleted_at__isnull=True).exists():
            issues.append(_reference_issue("depreciation_id"))
    if not _tags_exist(native.tag_ids, using=using):
        issues.append(_reference_issue("tag_ids"))
    if native.staged_image_id is not None:
        if preview_stage_or_none(native.staged_image_id, actor, CREATE_COMMAND_KIND, using=using, now=now) is None:
            issues.append(_reference_issue("staged_image_id"))
    return tuple(issues)


def _create_domain_issues(
    native: AssetTypeNativeCreateInputDTO,
    locks: _CreateDependencyLocks | None,
    actor: ActorContextDTO,
    *,
    using: str = _DEFAULT_DB,
    now=None,
) -> tuple[DomainIssueDTO, ...]:
    """Shared row-9 native/reference validation for the preview and write paths."""
    issues = list(_native_field_issues(native))
    slug_issue = _explicit_slug_conflict_issue(native, using=using)
    if slug_issue is not None:
        issues.append(slug_issue)
    if locks is None:
        issues.extend(_preview_reference_issues(native, actor, using=using, now=now))
    else:
        issues.extend(_locked_reference_issues(native, locks))
    return tuple(issues)


def _verify_create_token(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
    preview_token: str | None,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str | None,
    consuming: bool,
) -> CommandRejectedDTO | None:
    if preview_token is None:
        return None
    expectation = PreviewTokenExpectation(
        actor_id=actor.actor_id,
        authentication_revision=actor.authentication_revision,
        access_scope_fingerprint=None,
        command_kind=_CREATE_COMMAND_KIND,
        target=None,
        normalized_input_digest=_create_input_digest(native, fieldsets, patch),
        expected_resource_revision=None,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=(
            expected_category_default_snapshot_revision if consuming else None
        ),
        historical_state_digest=None,
    )
    try:
        verify_preview_token(preview_token, expected=expectation, key=_preview_token_key())
    except PreviewTokenError:
        return rejected(None, stale_plan_issue())
    return None


def _verify_apply_token(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
    patch: SpecificationPatchDTO,
    preview_token: str,
    expected_resource_revision: str,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str,
) -> CommandRejectedDTO | None:
    expectation = PreviewTokenExpectation(
        actor_id=actor.actor_id,
        authentication_revision=actor.authentication_revision,
        access_scope_fingerprint=None,
        command_kind=_APPLY_COMMAND_KIND,
        target=PreviewOwnerRef("asset_type", asset_type_id),
        normalized_input_digest=_apply_input_digest(patch),
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
        historical_state_digest=None,
    )
    try:
        verify_preview_token(preview_token, expected=expectation, key=_preview_token_key())
    except PreviewTokenError:
        return rejected(OwnerRefDTO("asset_type", asset_type_id), stale_plan_issue())
    return None


def _create_front_state(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
):
    """Locked row-2 preconditions: authorization, then references.

    Authorization is reloaded before any structural read so a
    broken/unresolvable graph can never disclose structure to an unauthorized
    caller.  The reference rows are locked in deterministic model-label/PK
    order.
    """
    actor_model = _reloaded_authorized_actor(actor, AssetType, _ADD_PERMISSION)
    if actor_model is None:
        return unavailable()
    proposed_identities = _proposed_identities_read(native, fieldsets)
    locks = _lock_create_references(native, actor)
    if isinstance(locks, CommandRejectedDTO):
        return locks
    return actor_model, proposed_identities, locks


def _create_back_state(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
    preview_token: str | None,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str | None,
    consuming: bool,
    proposed_identities: tuple[str, ...],
    locks: _CreateDependencyLocks,
):
    """Verified write state: token, revision gates, validation, desired rows."""
    token_rejection = _verify_create_token(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
        patch=patch,
        preview_token=preview_token,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
        consuming=consuming,
    )
    if token_rejection is not None:
        return token_rejection
    snapshot = None
    authoritative_identities = proposed_identities
    if consuming:
        snapshot = _snapshot_or_rejection(native.category_id, owner_ref=None)
        if isinstance(snapshot, CommandRejectedDTO):
            return snapshot
        if snapshot.revision != expected_category_default_snapshot_revision:
            return rejected(
                None,
                issue(
                    "STALE_RESOURCE",
                    path=("expected_category_default_snapshot_revision",),
                    message_key=_STALE_RESOURCE_MESSAGE,
                ),
            )
        authoritative_identities = _proposed_identities(native, fieldsets, snapshot)
    plan = _prospective_definition_or_rejection(
        authoritative_identities,
        stored_values={},
        owner_ref=None,
        expected_definition_revision=expected_definition_revision,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    definition, definitions, _graph = plan
    domain_issues = _create_domain_issues(native, locks, actor)
    if domain_issues:
        return rejected(None, *domain_issues)
    normalized = normalize_patch(
        patch,
        definitions,
        {},
        operation="create",
    )
    if isinstance(normalized, tuple):
        return rejected(None, *normalized)
    proposed_values = dict(normalized.stored_values)
    desired_ids = _desired_fieldset_ids(authoritative_identities, owner_ref=None)
    if isinstance(desired_ids, CommandRejectedDTO):
        return desired_ids
    return definition, proposed_values, desired_ids


def _desired_fieldset_ids(
    proposed_identities: Sequence[str],
    *,
    owner_ref: OwnerRefDTO | None,
):
    proposed_ids = fieldset_ids_for_identities(proposed_identities, using=_DEFAULT_DB)
    if any(fieldset_id is None for fieldset_id in proposed_ids):
        return _reference_rejection(owner_ref)
    return tuple(fieldset_id for fieldset_id in proposed_ids if fieldset_id is not None)


def _preview_create_plan(
    *,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    snapshot: CategoryDefaultSnapshotDTO | None,
    patch: SpecificationPatchDTO,
    actor: ActorContextDTO,
):
    proposed_identities = _proposed_identities(native, fieldsets, snapshot)
    plan = _prospective_definition_or_rejection(
        proposed_identities,
        stored_values={},
        owner_ref=None,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    definition, definitions, _graph = plan
    domain_issues = _create_domain_issues(native, locks=None, actor=actor)
    if domain_issues:
        return rejected(None, *domain_issues)
    normalized = normalize_patch(
        patch,
        definitions,
        {},
        operation="create",
    )
    issues = () if not isinstance(normalized, tuple) else normalized
    return definition, issues


def _preview_create_locked(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
) -> AssetTypePreviewResult:
    actor_model = _reloaded_authorized_actor(actor, AssetType, _ADD_PERMISSION)
    if actor_model is None:
        return unavailable()
    if _available_manufacturer(native.manufacturer_id) is None:
        return unavailable()
    if native.category_id is not None and _available_category(native.category_id) is None:
        return unavailable()

    consuming = _consumes_category_defaults(native, fieldsets)
    snapshot = None
    if consuming:
        snapshot = _snapshot_or_rejection(native.category_id, owner_ref=None)
        if isinstance(snapshot, CommandRejectedDTO):
            return snapshot
    plan_result = _preview_create_plan(
        native=native,
        fieldsets=fieldsets,
        snapshot=snapshot,
        patch=patch,
        actor=actor,
    )
    if isinstance(plan_result, CommandRejectedDTO):
        return plan_result
    definition, issues = plan_result
    expected_definition_revision = DefinitionRevision(definition.revision)

    if consuming:
        token = issue_preview_token(
            PreviewTokenExpectation(
                actor_id=actor.actor_id,
                authentication_revision=actor.authentication_revision,
                access_scope_fingerprint=None,
                command_kind=_CREATE_COMMAND_KIND,
                target=None,
                normalized_input_digest=_create_input_digest(native, fieldsets, patch),
                expected_resource_revision=None,
                expected_definition_revision=expected_definition_revision,
                expected_category_default_snapshot_revision=snapshot.revision,
                historical_state_digest=None,
            ),
            key=_preview_token_key(),
        )
        return AssetTypePreviewDTO(
            preview_token=PreviewToken(token),
            definition=definition,
            expected_definition_revision=expected_definition_revision,
            expected_resource_revision=None,
            expected_category_default_snapshot_revision=snapshot.revision,
            consumes_category_defaults=True,
            issues=issues,
        )
    return AssetTypePreviewDTO(
        preview_token=None,
        definition=definition,
        expected_definition_revision=expected_definition_revision,
        expected_resource_revision=None,
        expected_category_default_snapshot_revision=None,
        consumes_category_defaults=False,
        issues=issues,
    )


def preview_asset_type_create(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
) -> AssetTypePreviewResult:
    """Plan one Type create and sign a 30-minute preview token when defaults are consumed."""
    _validate_create_preview_inputs(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
        patch=patch,
    )
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _preview_create_locked(
                actor=actor,
                native=native,
                fieldsets=fieldsets,
                patch=patch,
            )


def _link_tags(owner: AssetType, tag_ids: tuple[int, ...]) -> None:
    """Link the active tags; kept as a seam so atomicity tests can inject failure."""
    owner.tags.set(tag_ids)


def _link_memberships(owner: AssetType, proposed_ids: tuple[int, ...]) -> None:
    """Insert the dense membership rows; kept as a seam so atomicity tests can inject failure."""
    AssetTypeFieldset.objects.using(_DEFAULT_DB).bulk_create(
        [
            AssetTypeFieldset(
                asset_type_id=owner.pk,
                fieldset_id=fieldset_id,
                position=position,
            )
            for position, fieldset_id in enumerate(proposed_ids, start=1)
        ]
    )


def _persist_create_owner(
    *,
    native: AssetTypeNativeCreateInputDTO,
    proposed_values: dict[str, object],
    proposed_ids: tuple[int, ...],
    actor_model: object,
    actor: ActorContextDTO,
    stage_row: Any = None,
):
    """Persist the Type, dense membership rows, tags, and stage consume atomically.

    The stored stage name is assigned to the owner's image field inside the
    same savepoint as owner, memberships, tags, and audit, so a later failure
    rolls the consume back and leaves the stage pending and untouched.  The
    stage is revalidated once more immediately before consumption so an
    expiry that elapsed while the command held its locks fails cleanly.
    """
    kwargs: dict[str, object] = {
        "manufacturer_id": native.manufacturer_id,
        "model": native.model,
        "slug": "" if native.slug is None else native.slug,
        "part_number": native.part_number,
        "ean": native.ean,
        "region": native.region,
        "configuration": native.configuration,
        "eol_months": native.eol_months,
        "category_id": native.category_id,
        "asset_role_id": native.suggested_asset_role_id,
        "depreciation_id": native.depreciation_id,
        "description": native.description,
        "comments": native.comments,
        "requestable": native.requestable,
        "custom_field_data": proposed_values,
    }
    if stage_row is not None:
        kwargs["image"] = stage_row.storage_key
    new_type = AssetType(**kwargs)
    try:
        with transaction.atomic(using=_DEFAULT_DB):
            with actor_change_context(actor_model):
                new_type.save(using=_DEFAULT_DB)
            if proposed_ids:
                _link_memberships(new_type, proposed_ids)
            if native.tag_ids:
                _link_tags(new_type, native.tag_ids)
            if stage_row is not None:
                final_stage = lock_stage_for_consume(
                    stage_row.stage_id,
                    actor,
                    CREATE_COMMAND_KIND,
                )
                if final_stage is None:
                    raise ValidationError("staged image is no longer consumable")
                consume_stage(final_stage, new_type.pk)
    except (ValidationError, IntegrityError):
        return rejected(
            None,
            issue("REFERENCE_CONFLICT", message_key=_REFERENCE_CONFLICT_MESSAGE),
        )
    return new_type


def _create_locked(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
    preview_token: str | None,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str | None,
) -> AssetTypeCreateResult:
    consuming = _consumes_category_defaults(native, fieldsets)
    front = _create_front_state(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
    )
    if isinstance(front, CommandRejectedDTO):
        return front
    actor_model, proposed_identities, locks = front
    back = _create_back_state(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
        patch=patch,
        preview_token=preview_token,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
        consuming=consuming,
        proposed_identities=proposed_identities,
        locks=locks,
    )
    if isinstance(back, CommandRejectedDTO):
        return back
    definition, proposed_values, desired_ids = back
    persisted = _persist_create_owner(
        native=native,
        proposed_values=proposed_values,
        proposed_ids=desired_ids,
        actor_model=actor_model,
        actor=actor,
        stage_row=locks.stage,
    )
    if isinstance(persisted, CommandRejectedDTO):
        return persisted
    return OwnerCreatedDTO(
        outcome="created",
        owner=OwnerRefDTO("asset_type", persisted.pk),
        resource_revision=resource_revision_for_owner(persisted),
        definition_revision=DefinitionRevision(definition.revision),
    )


def create_asset_type(
    *,
    actor: ActorContextDTO,
    native: AssetTypeNativeCreateInputDTO,
    fieldsets: FieldsetSelectionDTO,
    patch: SpecificationPatchDTO,
    preview_token: PreviewToken | None,
    expected_definition_revision: DefinitionRevision | None,
    expected_category_default_snapshot_revision: CategoryDefaultSnapshotRevision | None,
) -> AssetTypeCreateResult:
    """Create one Asset Type atomically under the exclusive catalogue lock."""
    _validate_create_inputs(
        actor=actor,
        native=native,
        fieldsets=fieldsets,
        patch=patch,
        preview_token=preview_token,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
    )
    missing = _missing_create_preconditions(
        native=native,
        fieldsets=fieldsets,
        preview_token=preview_token,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
    )
    if missing:
        return rejected(None, *missing)

    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(exclusive=True, using=_DEFAULT_DB):
            return _create_locked(
                actor=actor,
                native=native,
                fieldsets=fieldsets,
                patch=patch,
                preview_token=preview_token,
                expected_definition_revision=cast(DefinitionRevision, expected_definition_revision),
                expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
            )


def _apply_preview_plan(
    *,
    asset_type_id: int,
    owner: AssetType,
    snapshot: CategoryDefaultSnapshotDTO,
    patch: SpecificationPatchDTO,
    owner_ref: OwnerRefDTO,
):
    proposed_identities = tuple(str(membership.fieldset_identity) for membership in snapshot.memberships)
    stored_values = stored_values_for(owner)
    plan = _prospective_definition_or_rejection(
        proposed_identities,
        stored_values=stored_values,
        owner_ref=owner_ref,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    definition, definitions, _graph = plan
    normalized = normalize_patch(
        patch,
        definitions,
        stored_values,
        operation="composition_edit",
    )
    issues = () if not isinstance(normalized, tuple) else normalized
    return definition, issues


def _preview_apply_locked(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    expected_resource_revision: str,
    patch: SpecificationPatchDTO,
) -> AssetTypePreviewResult:
    owner = AssetType.objects.using(_DEFAULT_DB).filter(pk=asset_type_id, deleted_at__isnull=True).first()
    owner_ref = OwnerRefDTO("asset_type", asset_type_id)
    if owner is None:
        return unavailable()
    if owner.category_id is None:
        return unavailable()

    actor_model = _reloaded_authorized_actor(actor, AssetType, _CHANGE_PERMISSION)
    if actor_model is None:
        return unavailable()

    # Row-2 availability: a soft-deleted Category retained on the Type is
    # inaccessible and must never be previewed against its stale defaults.
    if _available_category(owner.category_id) is None:
        return unavailable()

    actual_resource_revision = resource_revision_for_owner(owner)
    if expected_resource_revision != actual_resource_revision:
        return rejected(
            owner_ref,
            issue("STALE_RESOURCE", path=("expected_resource_revision",), message_key=_STALE_RESOURCE_MESSAGE),
        )

    snapshot = _snapshot_or_rejection(owner.category_id, owner_ref=owner_ref)
    if isinstance(snapshot, CommandRejectedDTO):
        return snapshot
    plan = _apply_preview_plan(
        asset_type_id=asset_type_id,
        owner=owner,
        snapshot=snapshot,
        patch=patch,
        owner_ref=owner_ref,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    definition, issues = plan
    expected_definition_revision = DefinitionRevision(definition.revision)
    token = issue_preview_token(
        PreviewTokenExpectation(
            actor_id=actor.actor_id,
            authentication_revision=actor.authentication_revision,
            access_scope_fingerprint=None,
            command_kind=_APPLY_COMMAND_KIND,
            target=PreviewOwnerRef("asset_type", asset_type_id),
            normalized_input_digest=_apply_input_digest(patch),
            expected_resource_revision=expected_resource_revision,
            expected_definition_revision=expected_definition_revision,
            expected_category_default_snapshot_revision=snapshot.revision,
            historical_state_digest=None,
        ),
        key=_preview_token_key(),
    )
    return AssetTypePreviewDTO(
        preview_token=PreviewToken(token),
        definition=definition,
        expected_definition_revision=expected_definition_revision,
        expected_resource_revision=actual_resource_revision,
        expected_category_default_snapshot_revision=snapshot.revision,
        consumes_category_defaults=True,
        issues=issues,
    )


def preview_apply_category_defaults(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    expected_resource_revision: ResourceRevision,
    patch: SpecificationPatchDTO,
) -> AssetTypePreviewResult:
    """Plan an apply of the Type's Category defaults and sign a preview token."""
    _validate_apply_shared_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        patch=patch,
    )
    revision_string(expected_resource_revision, "expected_resource_revision")
    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(using=_DEFAULT_DB):
            return _preview_apply_locked(
                actor=actor,
                asset_type_id=asset_type_id,
                expected_resource_revision=expected_resource_revision,
                patch=patch,
            )


def _apply_front_state(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
):
    """Locked row-2 preconditions: authorization, owner, category.

    Authorization precedes every lock; the owner and Category rows are locked
    in stable ``(model identity, primary key)`` order.
    """
    actor_model = _reloaded_authorized_actor(actor, AssetType, _CHANGE_PERMISSION)
    if actor_model is None:
        return unavailable()
    owner = _lock_asset_type(asset_type_id)
    if owner is None:
        return unavailable()
    category_id = owner.category_id
    if category_id is None:
        return unavailable()
    if _lock_category(category_id) is None:
        return unavailable()
    owner_ref = OwnerRefDTO("asset_type", asset_type_id)
    return owner, actor_model, owner_ref


def _apply_back_state(
    *,
    actor: ActorContextDTO,
    asset_type_id: int,
    patch: SpecificationPatchDTO,
    preview_token: str,
    expected_resource_revision: str,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str,
    owner_ref: OwnerRefDTO,
    owner: AssetType,
):
    """Verified write state: token, then snapshot/resource/definition gates."""
    token_rejection = _verify_apply_token(
        actor=actor,
        asset_type_id=asset_type_id,
        patch=patch,
        preview_token=preview_token,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
    )
    if token_rejection is not None:
        return token_rejection
    snapshot = _snapshot_or_rejection(owner.category_id, owner_ref=owner_ref)
    if isinstance(snapshot, CommandRejectedDTO):
        return snapshot
    if snapshot.revision != expected_category_default_snapshot_revision:
        return rejected(
            owner_ref,
            issue(
                "STALE_RESOURCE",
                path=("expected_category_default_snapshot_revision",),
                message_key=_STALE_RESOURCE_MESSAGE,
            ),
        )
    actual_resource_revision = resource_revision_for_owner(owner)
    if expected_resource_revision != actual_resource_revision:
        return rejected(
            owner_ref,
            issue("STALE_RESOURCE", path=("expected_resource_revision",), message_key=_STALE_RESOURCE_MESSAGE),
        )
    proposed_identities = tuple(str(membership.fieldset_identity) for membership in snapshot.memberships)
    plan = _prospective_definition_or_rejection(
        proposed_identities,
        stored_values=stored_values_for(owner),
        owner_ref=owner_ref,
        expected_definition_revision=expected_definition_revision,
    )
    if isinstance(plan, CommandRejectedDTO):
        return plan
    proposed_definition, proposed_definitions, _graph = plan
    normalized = normalize_patch(
        patch,
        proposed_definitions,
        stored_values_for(owner),
        operation="composition_edit",
    )
    if isinstance(normalized, tuple):
        return rejected(owner_ref, *normalized)
    proposed_values = dict(normalized.stored_values)
    desired_ids = _desired_fieldset_ids(proposed_identities, owner_ref=owner_ref)
    if isinstance(desired_ids, CommandRejectedDTO):
        return desired_ids
    return proposed_definition, proposed_values, desired_ids, actual_resource_revision


def _apply_locked(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    preview_token: str,
    expected_resource_revision: str,
    expected_definition_revision: str,
    expected_category_default_snapshot_revision: str,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    front = _apply_front_state(
        actor=actor,
        asset_type_id=asset_type_id,
    )
    if isinstance(front, CommandRejectedDTO):
        return front
    owner, actor_model, owner_ref = front
    back = _apply_back_state(
        actor=actor,
        asset_type_id=asset_type_id,
        patch=patch,
        preview_token=preview_token,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
        owner_ref=owner_ref,
        owner=owner,
    )
    if isinstance(back, CommandRejectedDTO):
        return back
    proposed_definition, proposed_values, desired_ids, actual_resource_revision = back

    current_rows = _current_membership_rows(AssetType, asset_type_id, using=_DEFAULT_DB)
    desired_rows = tuple((fieldset_id, position) for position, fieldset_id in enumerate(desired_ids, start=1))
    current_pairs = tuple((row["fieldset_id"], row["position"]) for row in current_rows)
    membership_changed = current_pairs != desired_rows
    values_changed = not json_values_equal(owner.custom_field_data, proposed_values)
    if not membership_changed and not values_changed:
        return OwnerNoOpDTO(
            outcome="no_op",
            owner=owner_ref,
            resource_revision=actual_resource_revision,
            definition_revision=proposed_definition.revision,
        )

    rejection = _persist_replacement(
        owner=owner,
        owner_model=AssetType,
        owner_id=asset_type_id,
        fieldset_ids=desired_ids,
        values_changed=values_changed,
        proposed_values=proposed_values,
        membership_changed=membership_changed,
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


def apply_category_defaults(
    *,
    actor: ActorContextDTO,
    asset_type_id: AssetTypeId,
    preview_token: PreviewToken,
    expected_resource_revision: ResourceRevision,
    expected_definition_revision: DefinitionRevision,
    expected_category_default_snapshot_revision: CategoryDefaultSnapshotRevision,
    patch: SpecificationPatchDTO,
) -> OwnerMutationResult:
    """Apply one Type's Category default composition and optional patch atomically."""
    _validate_apply_shared_inputs(
        actor=actor,
        asset_type_id=asset_type_id,
        patch=patch,
    )
    missing = _missing_apply_preconditions(
        preview_token=preview_token,
        expected_resource_revision=expected_resource_revision,
        expected_definition_revision=expected_definition_revision,
        expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
    )
    if missing:
        return rejected(None, *missing)
    revision_string(expected_resource_revision, "expected_resource_revision")
    revision_string(expected_definition_revision, "expected_definition_revision")
    revision_string(
        expected_category_default_snapshot_revision,
        "expected_category_default_snapshot_revision",
    )
    if type(preview_token) is not str:
        raise TypeError("preview_token must be a string")

    with transaction.atomic(using=_DEFAULT_DB):
        with catalogue_transaction_lock(exclusive=True, using=_DEFAULT_DB):
            return _apply_locked(
                actor=actor,
                asset_type_id=asset_type_id,
                preview_token=preview_token,
                expected_resource_revision=expected_resource_revision,
                expected_definition_revision=expected_definition_revision,
                expected_category_default_snapshot_revision=expected_category_default_snapshot_revision,
                patch=patch,
            )


__all__ = [
    "apply_category_defaults",
    "create_asset_type",
    "preview_apply_category_defaults",
    "preview_asset_type_create",
]
