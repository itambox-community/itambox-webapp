"""Native-reference liveness and real REST-delete/public-create PostgreSQL races.

REST opponents dispatch the supported ViewSet destroy action (including permissions,
ETag checks, row locking and model soft deletion), not raw SQL or mocked querysets.
Request authentication and audit context are supplied by the harness, not middleware.
"""

import threading
import time
from dataclasses import replace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from rest_framework.test import APIRequestFactory, force_authenticate

from assets.api.views import AssetRoleViewSet, DepreciationViewSet
from assets.models.catalog import AssetRole, AssetType, AssetTypeFieldset, Depreciation
from assets.services.specifications.contracts import AssetTypePreviewDTO, CommandRejectedDTO, OwnerCreatedDTO
from assets.tests.test_specification_create_races import _create, _create_preview, _finish, _native, _start
from core.models import ObjectChange
from core.tasks.context import TaskContext
from extras.api.views import TagViewSet
from extras.models import Tag

pytest_plugins = ("assets.tests.test_specification_create_races",)
pytestmark = [pytest.mark.serial_only, pytest.mark.django_db(transaction=True)]
_REFERENCE_KINDS = ("role", "depreciation", "tag")


@pytest.fixture
def reference_kit(create_race_kit):
    manufacturer, category, first, _second, actor, provider_id = create_race_kit
    role = AssetRole.objects.create(name="Reference role", slug="reference-role")
    depreciation = Depreciation.objects.create(name="Reference depreciation", months=12)
    tag = Tag.objects.create(name="Reference tag", slug="reference-tag")
    survivor = Tag.objects.create(name="Surviving tag", slug="surviving-tag")
    deleter = get_user_model().objects.create_superuser(username="reference-deleter", password=None)
    native = replace(
        _native(manufacturer, category, model="Reference race type"),
        suggested_asset_role_id=role.pk,
        depreciation_id=depreciation.pk,
        tag_ids=(tag.pk, survivor.pk),
    )
    return {
        "actor": actor,
        "provider_id": provider_id,
        "native": native,
        "first": first,
        "deleter": deleter,
        "role": (role, AssetRoleViewSet, "suggested_asset_role_id"),
        "depreciation": (depreciation, DepreciationViewSet, "depreciation_id"),
        "tag": (tag, TagViewSet, "tag_ids"),
    }


def _delete_reference(kit, kind):
    reference, viewset, _path = kit[kind]
    request = APIRequestFactory().delete(
        "/reference-delete/", {}, format="json", HTTP_IF_MATCH=viewset._get_etag(reference)
    )
    user = get_user_model().objects.get(pk=kit["deleter"].pk)
    force_authenticate(request, user=user)
    with TaskContext(user_id=user.pk, operation="native_reference_delete_test"):
        response = viewset.as_view({"delete": "destroy"})(request, pk=reference.pk)
    assert response.status_code == 204, getattr(response, "data", None)
    return response.status_code


def _changes(model):
    return ObjectChange._base_manager.filter(changed_object_type=ContentType.objects.get_for_model(model))


def _assert_no_create_effects():
    assert AssetType.all_objects.count() == 0
    assert AssetTypeFieldset.objects.count() == 0
    assert AssetType.tags.through.objects.count() == 0
    assert _changes(AssetType).count() == 0
    assert _changes(AssetTypeFieldset).count() == 0


def _assert_rejected(result, kit, kind):
    assert isinstance(result, CommandRejectedDTO)
    assert result.safe_owner is None
    assert [(issue.code, issue.path) for issue in result.issues] == [("REFERENCE_CONFLICT", (kit[kind][2],))]
    _assert_no_create_effects()


def _assert_created(result, kit):
    assert isinstance(result, OwnerCreatedDTO)
    owner = AssetType.all_objects.get(pk=result.owner.owner_id)
    assert AssetType.all_objects.count() == 1
    assert owner.deleted_at is None
    assert owner.asset_role_id == kit["native"].suggested_asset_role_id
    assert owner.depreciation_id == kit["native"].depreciation_id
    assert list(owner.fieldset_memberships.values_list("fieldset_id", "position")) == [(kit["first"].pk, 1)]
    assert AssetTypeFieldset.objects.count() == 1
    # Soft deletion never cascades (#619): a deleted Tag keeps its link rows,
    # exactly like Role/Depreciation keep their nullable FKs.
    expected_tags = kit["native"].tag_ids
    assert set(AssetType.tags.through.objects.values_list("assettype_id", "tag_id")) == {
        (owner.pk, pk) for pk in expected_tags
    }
    assert list(_changes(AssetType).values_list("changed_object_id", "action", "user_id")) == [
        (owner.pk, "create", kit["actor"].actor_id)
    ]
    # Real rows remain behind every FK/M2M link: no orphan accepted on commit.
    assert AssetRole.all_objects.filter(pk=owner.asset_role_id).exists()
    assert Depreciation.all_objects.filter(pk=owner.depreciation_id).exists()
    assert Tag.all_objects.filter(pk__in=kit["native"].tag_ids).count() == 2


def _assert_reference_state(kit, kind, *, deleted):
    reference = kit[kind][0]
    reference.refresh_from_db()
    assert (reference.deleted_at is not None) == deleted
    changes = _changes(type(reference)).filter(changed_object_id=reference.pk)
    assert list(changes.values_list("action", "user_id")) == ([("delete", kit["deleter"].pk)] if deleted else [])


def _assert_reference_wait(pid, reference):
    """Observe the exact opponent SELECT FOR UPDATE, blocking PID and locks."""
    deadline = time.monotonic() + 10
    last = None
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_stat_clear_snapshot()")
            cursor.execute(
                "SELECT pg_backend_pid(), wait_event_type, wait_event, pg_blocking_pids(pid), query "
                "FROM pg_stat_activity WHERE pid = %s",
                [pid],
            )
            last = cursor.fetchone()
            if last and last[1] == "Lock" and last[0] in last[3]:
                assert pid != last[0]
                assert reference._meta.db_table in last[4]
                assert "FOR UPDATE" in last[4]
                cursor.execute(
                    "SELECT locktype, mode, granted FROM pg_locks WHERE pid = %s "
                    "AND (relation = %s::regclass OR (NOT granted AND locktype IN ('tuple', 'transactionid')))",
                    [pid, reference._meta.db_table],
                )
                locks = cursor.fetchall()
                assert any(row[0] == "relation" and row[2] for row in locks)
                assert any(row[0] in ("tuple", "transactionid") and not row[2] for row in locks)
                print("OBSERVED_NATIVE_REFERENCE_WAIT", reference._meta.label, reference.pk, pid, last, locks)
                return
        threading.Event().wait(0.01)
    pytest.fail(f"backend {pid} never reached reference row wait: {last}")


def test_sequential_tag_delete_keeps_its_link_after_create(reference_kit):
    """Control for the no-cascade soft-delete semantics (#619), not a race repair."""
    kit = reference_kit
    preview = _create_preview(kit["actor"], kit["native"])
    assert isinstance(preview, AssetTypePreviewDTO)
    result = _create(kit["actor"], kit["native"], preview)
    _assert_created(result, kit)
    before = list(_changes(AssetType).values_list("pk", flat=True))
    _delete_reference(kit, "tag")
    owner = AssetType.all_objects.get(pk=result.owner.owner_id)
    assert owner.deleted_at is None
    assert owner.asset_role_id == kit["native"].suggested_asset_role_id
    assert owner.depreciation_id == kit["native"].depreciation_id
    assert set(AssetType.tags.through.objects.values_list("assettype_id", "tag_id")) == {
        (owner.pk, pk) for pk in kit["native"].tag_ids
    }
    assert list(owner.fieldset_memberships.values_list("fieldset_id", "position")) == [(kit["first"].pk, 1)]
    assert list(_changes(AssetType).values_list("pk", flat=True)) == before
    _assert_reference_state(kit, "tag", deleted=True)


@pytest.mark.parametrize("kind", _REFERENCE_KINDS)
def test_soft_deleted_reference_preview_write_parity(reference_kit, kind):
    kit = reference_kit
    # Non-default-consuming create has no token input binding; a matching
    # definition precondition permits ordinary liveness validation in both paths.
    native = replace(kit["native"], category_id=None)
    plan = _create_preview(kit["actor"], native)
    assert isinstance(plan, AssetTypePreviewDTO)
    assert plan.preview_token is None
    _delete_reference(kit, kind)
    before = list(ObjectChange._base_manager.values_list("pk", flat=True))
    _assert_rejected(_create_preview(kit["actor"], native), kit, kind)
    _assert_rejected(_create(kit["actor"], native, plan), kit, kind)
    assert list(ObjectChange._base_manager.values_list("pk", flat=True)) == before
    _assert_reference_state(kit, kind, deleted=True)


@pytest.mark.parametrize("kind", _REFERENCE_KINDS)
def test_reference_deleted_after_signed_preview_rejects_without_side_effects(reference_kit, kind):
    kit = reference_kit
    preview = _create_preview(kit["actor"], kit["native"])
    assert isinstance(preview, AssetTypePreviewDTO)
    assert preview.preview_token is not None
    _delete_reference(kit, kind)
    before = list(ObjectChange._base_manager.values_list("pk", flat=True))
    _assert_rejected(_create(kit["actor"], kit["native"], preview), kit, kind)
    assert list(ObjectChange._base_manager.values_list("pk", flat=True)) == before
    _assert_reference_state(kit, kind, deleted=True)


@pytest.mark.parametrize("kind", _REFERENCE_KINDS)
@pytest.mark.parametrize("rollback_delete", [False, True])
def test_create_waits_for_rest_reference_delete_then_rechecks_liveness(reference_kit, kind, rollback_delete):
    kit = reference_kit
    preview = _create_preview(kit["actor"], kit["native"])
    assert isinstance(preview, AssetTypePreviewDTO)
    started = None
    try:
        with transaction.atomic():
            _delete_reference(kit, kind)
            started = _start(lambda: _create(kit["actor"], kit["native"], preview), kit["provider_id"])
            _assert_reference_wait(started[1], kit[kind][0])
            assert not started[2] and not started[3]
            transaction.set_rollback(rollback_delete)
    finally:
        if started is not None:
            result = _finish(started)
    if rollback_delete:
        _assert_created(result, kit)
    else:
        _assert_rejected(result, kit, kind)
    _assert_reference_state(kit, kind, deleted=not rollback_delete)


@pytest.mark.parametrize("kind", _REFERENCE_KINDS)
@pytest.mark.parametrize("rollback_create", [False, True])
def test_rest_reference_delete_waits_for_create_transaction(reference_kit, kind, rollback_create):
    kit = reference_kit
    preview = _create_preview(kit["actor"], kit["native"])
    assert isinstance(preview, AssetTypePreviewDTO)
    started = None
    try:
        with transaction.atomic():
            result = _create(kit["actor"], kit["native"], preview)
            _assert_created(result, kit)
            started = _start(lambda: _delete_reference(kit, kind), kit["provider_id"])
            _assert_reference_wait(started[1], kit[kind][0])
            assert not started[2] and not started[3]
            transaction.set_rollback(rollback_create)
    finally:
        if started is not None:
            assert _finish(started) == 204
    if rollback_create:
        _assert_no_create_effects()
    else:
        _assert_created(result, kit)
    _assert_reference_state(kit, kind, deleted=True)
