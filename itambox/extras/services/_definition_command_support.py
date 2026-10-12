"""Shared ORM mechanics for globally authorized definition commands."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, models

from assets.services.specifications._command_support import actor_change_context
from assets.services.specifications.contracts import DomainIssueDTO
from assets.services.specifications.locking import catalogue_transaction_lock
from extras.models import (
    CustomField,
    CustomFieldChoice,
    CustomFieldChoiceSet,
    CustomFieldset,
    CustomFieldsetField,
)
from extras.services.definition_command_contracts import DefinitionRejectedDTO, DefinitionSuccessDTO
from extras.services.specifications.contracts import QualifiedIdentity, ResourceRevision
from organization.services.access_scope import ActorContextDTO, authentication_revision_for_actor
from organization.services.catalogue_authorization import has_provider_catalogue_permission

_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*/[a-z0-9][a-z0-9._-]{0,126}$")
_CHOICE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,62}$")
_RESERVED_NAMESPACES = frozenset({"itambox", "catalog"})


class DefinitionCommandError(Exception):
    """Internal expected rejection carrying stable domain issues."""

    def __init__(self, *issues: DomainIssueDTO) -> None:
        super().__init__("definition command rejected")
        self.issues = tuple(issues)


def issue(
    code: str,
    *,
    path: Sequence[str] = (),
    message_key: str | None = None,
) -> DomainIssueDTO:
    return DomainIssueDTO(
        code=code,  # type: ignore[arg-type]
        path=tuple(path),
        field_key=None,
        message_key=message_key or f"specifications.{code.lower()}",
    )


def ensure_actor(actor: ActorContextDTO) -> None:
    if not isinstance(actor, ActorContextDTO):
        raise TypeError("actor must be an ActorContextDTO")


def reload_actor(actor: ActorContextDTO, *, using: str = DEFAULT_DB_ALIAS):
    user_model = get_user_model()
    # unscoped: actor re-read by pk for authorization; user rows are not tenant-owned
    candidate = user_model._base_manager.using(using).filter(pk=actor.actor_id, is_active=True).first()
    if candidate is None:
        return None
    if authentication_revision_for_actor(candidate) != actor.authentication_revision:
        return None
    return candidate


def authorize_locked(actor: ActorContextDTO, model: type[models.Model], codename: str, *, using: str):
    principal = reload_actor(actor, using=using)
    permission = f"{model._meta.app_label}.{codename}"
    if principal is None or not has_provider_catalogue_permission(principal, permission):
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    return principal


def require_revision(value: object, name: str = "expected_resource_revision") -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def require_positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{name} must be a positive integer")
    return value


def validate_namespace(namespace: str) -> None:
    if type(namespace) is not str or _NAMESPACE_RE.fullmatch(namespace) is None:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=("namespace",)))
    if len(namespace) > 62:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=("namespace",)))
    if namespace in _RESERVED_NAMESPACES:
        raise DefinitionCommandError(issue("IMMUTABLE_DEFINITION", path=("namespace",)))


def validate_field_key(local_key: str, namespace: str) -> str:
    if type(local_key) is not str or _FIELD_KEY_RE.fullmatch(local_key) is None:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=("local_key",)))
    name = f"{namespace.replace('-', '_')}__{local_key}"
    if len(name) > 64 or _FIELD_KEY_RE.fullmatch(name) is None:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=("local_key",)))
    return name


def validate_qualified_identity(value: str, path: str) -> tuple[str, str]:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=(path,)))
    namespace, local = value.split("/", 1)
    return namespace, local


def validate_choice_identity(value: QualifiedIdentity | str, path: str) -> tuple[str, str, str]:
    if type(value) is not str or value.count("#") != 1:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=(path,)))
    owner_identity, key = value.rsplit("#", 1)
    namespace, slug = validate_qualified_identity(owner_identity, path)
    if _CHOICE_KEY_RE.fullmatch(key) is None:
        raise DefinitionCommandError(issue("INVALID_TYPE", path=(path,)))
    return namespace, slug, key


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(nested) for key, nested in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    return str(value)


def mapping_values(mappings: tuple[object, ...]) -> list[object]:
    return [_canonical(item) for item in mappings]


def _definition_payload(definition: object, using: str) -> dict[str, object]:
    payload: dict[str, object] = {"model": definition._meta.label_lower, "id": definition.pk, "version": 1}
    for field in definition._meta.concrete_fields:
        if field.name not in {"created_at", "updated_at"}:
            payload[field.name] = _canonical(getattr(definition, field.attname))
    if isinstance(definition, CustomField):
        payload["object_types"] = list(
            definition.object_types.using(using).order_by("app_label", "model").values_list("app_label", "model")
        )
    elif isinstance(definition, CustomFieldset):
        payload["memberships"] = list(
            CustomFieldsetField.objects.using(using)
            .filter(fieldset_id=definition.pk)
            .order_by("position", "custom_field_id")
            .values("custom_field_id", "position")
        )
    elif isinstance(definition, CustomFieldChoiceSet):
        payload["choices"] = list(
            CustomFieldChoice.objects.using(using)
            .filter(choice_set_id=definition.pk)
            .order_by("position", "key")
            .values("key", "label", "position", "lifecycle", "version", "replaced_by")
        )
    return payload


def resource_revision_for_definition(definition: object, *, using: str = DEFAULT_DB_ALIAS) -> ResourceRevision:
    serialized = json.dumps(
        _definition_payload(definition, using), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return ResourceRevision("sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest())


def identity_for(definition: object) -> QualifiedIdentity:
    if isinstance(definition, CustomField):
        return QualifiedIdentity(f"{definition.namespace}/{definition.name}")
    if isinstance(definition, (CustomFieldset, CustomFieldChoiceSet)):
        return QualifiedIdentity(f"{definition.namespace}/{definition.slug}")
    if isinstance(definition, CustomFieldChoice):
        choice_set = definition.choice_set
        return QualifiedIdentity(f"{choice_set.namespace}/{choice_set.slug}#{definition.key}")
    raise TypeError(f"unsupported definition model: {type(definition)!r}")


def model_kind(definition: object) -> str:
    if isinstance(definition, CustomField):
        return "field"
    if isinstance(definition, CustomFieldset):
        return "fieldset"
    if isinstance(definition, CustomFieldChoiceSet):
        return "choice_set"
    if isinstance(definition, CustomFieldChoice):
        return "choice"
    raise TypeError(f"unsupported definition model: {type(definition)!r}")


def success(definition: object, outcome: str, *, using: str = DEFAULT_DB_ALIAS):
    return DefinitionSuccessDTO(
        outcome=outcome,  # type: ignore[arg-type]
        definition_kind=model_kind(definition),  # type: ignore[arg-type]
        definition_id=definition.pk,
        identity=identity_for(definition),
        resource_revision=resource_revision_for_definition(definition, using=using),
        lifecycle=definition.lifecycle,
        version=definition.version,
    )


def rejected(*, kind: str | None, definition_id: int | None, identity: str | None, issues: Iterable[DomainIssueDTO]):
    return DefinitionRejectedDTO(
        outcome="rejected",
        definition_kind=kind,  # type: ignore[arg-type]
        definition_id=definition_id,
        identity=identity,  # type: ignore[arg-type]
        issues=tuple(issues),
    )


def reject_for(definition: object | None, *issues: DomainIssueDTO):
    if definition is None:
        return rejected(kind=None, definition_id=None, identity=None, issues=issues)
    return rejected(
        kind=model_kind(definition),
        definition_id=definition.pk,
        identity=identity_for(definition),
        issues=issues,
    )


def map_validation_error(error: ValidationError) -> tuple[DomainIssueDTO, ...]:
    if hasattr(error, "message_dict"):
        paths = tuple(sorted(error.message_dict))
    else:
        paths = ("definition",)
    return tuple(issue("REFERENCE_CONFLICT", path=(path,)) for path in paths)


def map_database_error(_: IntegrityError) -> tuple[DomainIssueDTO, ...]:
    return (issue("REFERENCE_CONFLICT"),)


def resolve_content_types(object_type_names: tuple[str, ...], *, using: str) -> list[ContentType]:
    if type(object_type_names) is not tuple or not object_type_names:
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("object_types",)))
    content_types: list[ContentType] = []
    seen: set[tuple[str, str]] = set()
    for raw_name in object_type_names:
        if type(raw_name) is not str or raw_name.count(".") != 1:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("object_types",)))
        app_label, model = raw_name.split(".", 1)
        identity = (app_label, model)
        if identity in seen:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("object_types",)))
        seen.add(identity)
        content_type = ContentType.objects.using(using).filter(app_label=app_label, model=model).first()
        if content_type is None:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("object_types",)))
        content_types.append(content_type)
    return content_types


def resolve_field_identities(identities: tuple[QualifiedIdentity, ...], *, using: str) -> list[CustomField]:
    if type(identities) is not tuple:
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("field_identities",)))
    fields: list[CustomField] = []
    seen: set[int] = set()
    for raw_identity in identities:
        namespace, name = validate_qualified_identity(raw_identity, "field_identities")
        field = CustomField.objects.using(using).filter(namespace=namespace, name=name).first()
        if field is None or field.pk in seen or field.lifecycle != CustomField.LIFECYCLE_ACTIVE:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("field_identities",)))
        seen.add(field.pk)
        fields.append(field)
    return fields


def _replacement_target(model: type[object], replacement: str, *, using: str) -> object | None:
    namespace, local = validate_qualified_identity(replacement, "replacement_identity")
    if model is CustomField:
        return CustomField.objects.using(using).filter(namespace=namespace, name=local).first()
    return model.objects.using(using).filter(namespace=namespace, slug=local).first()  # type: ignore[attr-defined]


def _ensure_replacement_chain_is_acyclic(
    model: type[object],
    source: object | None,
    target: object,
    *,
    using: str,
) -> None:
    seen: set[int] = set()
    current: object | None = target
    while current is not None:
        current_id = current.pk
        if (source is not None and current_id == source.pk) or current_id in seen:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
        seen.add(current_id)
        replacement = getattr(current, "replaced_by", None)
        if replacement is None:
            return
        current = _replacement_target(model, replacement, using=using)
        if current is None or current.lifecycle != current.LIFECYCLE_ACTIVE:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))


def resolve_replacement(model: type[object], source: object | None, replacement: str | None, *, using: str):
    if replacement is None:
        return None
    target = _replacement_target(model, replacement, using=using)
    if target is None or target.lifecycle != target.LIFECYCLE_ACTIVE or (source is not None and target.pk == source.pk):
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
    _ensure_replacement_chain_is_acyclic(model, source, target, using=using)
    return target


def _choice_replacement_target_for_identity(
    choice_set: CustomFieldChoiceSet,
    replacement: str,
    *,
    using: str,
) -> CustomFieldChoice | None:
    namespace, slug, key = validate_choice_identity(replacement, "replacement_identity")
    if (namespace, slug) != (choice_set.namespace, choice_set.slug):
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
    return CustomFieldChoice.objects.using(using).filter(choice_set_id=choice_set.pk, key=key).first()


def _ensure_choice_replacement_chain_is_acyclic(
    choice_set: CustomFieldChoiceSet,
    source: CustomFieldChoice | None,
    target: CustomFieldChoice,
    *,
    using: str,
) -> None:
    seen: set[int] = set()
    current: CustomFieldChoice | None = target
    while current is not None:
        if (source is not None and current.pk == source.pk) or current.pk in seen:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
        seen.add(current.pk)
        replacement = current.replaced_by
        if replacement is None:
            return
        current = _choice_replacement_target_for_identity(choice_set, replacement, using=using)
        if current is None or current.lifecycle != CustomFieldChoice.LIFECYCLE_ACTIVE:
            raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))


def resolve_choice_replacement(
    choice_set: CustomFieldChoiceSet,
    source: CustomFieldChoice | None,
    replacement: QualifiedIdentity | str | None,
    *,
    using: str,
) -> CustomFieldChoice | None:
    if replacement is None:
        return None
    target = _choice_replacement_target_for_identity(choice_set, replacement, using=using)
    if target is None or target.lifecycle != CustomFieldChoice.LIFECYCLE_ACTIVE:
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
    if source is not None and target.pk == source.pk:
        raise DefinitionCommandError(issue("REFERENCE_CONFLICT", path=("replacement_identity",)))
    _ensure_choice_replacement_chain_is_acyclic(choice_set, source, target, using=using)
    return target


def lock_rows(model: type[object], ids: Iterable[int], *, using: str) -> list[object]:
    values = sorted({require_positive_id(value, "definition id") for value in ids})
    if not values:
        return []
    return list(model.objects.using(using).select_for_update().filter(pk__in=values).order_by("pk"))  # type: ignore[attr-defined]


def lock_one(model: type[object], definition_id: int, *, using: str):
    locked = lock_rows(model, (definition_id,), using=using)
    if not locked:
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    return locked[0]


def lock_field_dependencies(field_id: int, *, using: str) -> CustomField:
    preview = CustomField.objects.using(using).filter(pk=field_id).values("choice_set_id").first()
    if preview is None:
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    if preview["choice_set_id"] is not None:
        lock_rows(CustomFieldChoiceSet, (preview["choice_set_id"],), using=using)
    return lock_one(CustomField, field_id, using=using)  # type: ignore[return-value]


def lock_choice_dependencies(choice_id: int, *, using: str) -> tuple[CustomFieldChoiceSet, CustomFieldChoice]:
    choice_set_id = (
        CustomFieldChoice.objects.using(using).filter(pk=choice_id).values_list("choice_set_id", flat=True).first()
    )
    if choice_set_id is None:
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    choice_sets = lock_rows(CustomFieldChoiceSet, (choice_set_id,), using=using)
    choices = lock_rows(CustomFieldChoice, (choice_id,), using=using)
    if not choices:
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    if not choice_sets:
        raise DefinitionCommandError(issue("OBJECT_UNAVAILABLE", message_key="specifications.object_unavailable"))
    return choice_sets[0], choices[0]  # type: ignore[return-value]


def validate_local_definition(definition: object) -> None:
    if getattr(definition, "management_kind", CustomField.MANAGEMENT_LOCAL) != CustomField.MANAGEMENT_LOCAL:
        raise DefinitionCommandError(issue("IMMUTABLE_DEFINITION"))


def save_definition(definition: object, principal: object, *, using: str) -> None:
    with actor_change_context(principal):
        definition.save(using=using)


def close_command_error(definition: object | None, error: DefinitionCommandError):
    if any(item.code == "OBJECT_UNAVAILABLE" for item in error.issues):
        definition = None
    return reject_for(definition, *error.issues)


def close_validation_error(definition: object | None, error: ValidationError):
    return reject_for(definition, *map_validation_error(error))


def close_integrity_error(definition: object | None, error: IntegrityError):
    return reject_for(definition, *map_database_error(error))


__all__ = [
    "DefinitionCommandError",
    "authorize_locked",
    "catalogue_transaction_lock",
    "close_command_error",
    "close_integrity_error",
    "close_validation_error",
    "identity_for",
    "issue",
    "lock_choice_dependencies",
    "lock_field_dependencies",
    "lock_one",
    "lock_rows",
    "model_kind",
    "reject_for",
    "reload_actor",
    "require_positive_id",
    "require_revision",
    "resolve_content_types",
    "resolve_choice_replacement",
    "resolve_field_identities",
    "resolve_replacement",
    "resource_revision_for_definition",
    "save_definition",
    "success",
    "validate_field_key",
    "validate_choice_identity",
    "validate_local_definition",
    "validate_namespace",
]
