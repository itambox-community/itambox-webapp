"""Unit contracts for the kernel slug and tenant-scope leaves (issue #100)."""

import csv
from types import ModuleType, SimpleNamespace
from unittest import mock

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import models
from django.test import SimpleTestCase

from core import tenant_scope as tenant_scope_module
from core.authorization_cache import invalidate_user_authorization_cache
from core.forms.import_forms import (
    BulkImportForm,
    get_import_form_class,
    is_model_importable,
    register_import_form,
    resolve_related,
)
from core.slugs import generate_unique_slug


class _Manager:
    def __init__(self, existing_results):
        self._existing_results = list(existing_results)
        self.filter_calls = []

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        return self

    def exclude(self, **kwargs):
        return self

    def exists(self):
        if self._existing_results:
            return self._existing_results.pop(0)
        return False


class _SlugModel:
    _base_manager = None
    objects = None
    _slug_field = models.SlugField(max_length=255)
    _meta = SimpleNamespace(get_field=lambda field_name: _SlugModel._slug_field)

    def __init__(self, slug=None, source="Plain Name"):
        self.slug = slug
        self.slug_source = source
        self.pk = None


class _ImportField:
    def __init__(
        self,
        name,
        *,
        primary_key=False,
        editable=True,
        blank=False,
        null=False,
        relation=False,
        related_model=None,
    ):
        self.name = name
        self.primary_key = primary_key
        self.auto_created = False
        self.editable = editable
        self.blank = blank
        self.null = null
        self.default = models.NOT_PROVIDED
        self.is_relation = relation
        self.many_to_one = relation
        self.related_model = related_model
        self.attname = f"{name}_id" if relation else name


class _ImportMeta:
    app_label = "kernelleaves"
    model_name = "importmodel"
    label_lower = "kernelleaves.importmodel"

    def __init__(self, fields):
        self.fields = fields
        self.pk = next(field for field in fields if field.primary_key)

    def get_field(self, name):
        for field in self.fields:
            if field.name == name:
                return field
        raise FieldDoesNotExist(name)


class _ImportKeyManager:
    """Manager stand-in whose ``filter`` yields the matching rows (a plain list)."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.filter_calls = []

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        return self.rows


class _ImportRelatedManager:
    def __init__(self, related_object=None):
        self.related_object = related_object

    def filter(self, **kwargs):
        return self

    def first(self):
        return self.related_object

    def get(self, **kwargs):
        if self.related_object is None:
            raise _ImportModel.DoesNotExist
        return self.related_object


class _ImportModel:
    DoesNotExist = type("DoesNotExist", (Exception,), {})
    objects = None
    _meta = None

    def __init__(self, **values):
        self.__dict__.update(values)
        self.saved = False
        self.cleaned = False
        self.snapshotted = False

    def full_clean(self):
        self.cleaned = True

    def save(self):
        self.saved = True

    def snapshot(self):
        self.snapshotted = True


class KernelSlugTests(SimpleTestCase):
    def test_existing_slug_is_left_untouched(self):
        obj = _SlugModel(slug="already-set")
        generate_unique_slug(obj)
        self.assertEqual(obj.slug, "already-set")

    def test_explicit_overlong_slug_remains_invalid(self):
        explicit_slug = "a" * 256
        obj = _SlugModel(slug=explicit_slug)
        generate_unique_slug(obj)
        self.assertEqual(obj.slug, explicit_slug)
        with self.assertRaises(ValidationError):
            obj._meta.get_field("slug").clean(obj.slug, None)

    def test_collision_appends_counter(self):
        obj = _SlugModel(slug="", source="Asset")
        previous = _SlugModel._base_manager
        _SlugModel._base_manager = _Manager([True, False])
        try:
            generate_unique_slug(obj, "slug_source")
        finally:
            _SlugModel._base_manager = previous
        self.assertEqual(obj.slug, "asset-1")

    def test_no_collision_keeps_base_slug(self):
        obj = _SlugModel(slug="", source="Asset")
        previous = _SlugModel._base_manager
        _SlugModel._base_manager = _Manager([False])
        try:
            generate_unique_slug(obj, "slug_source")
        finally:
            _SlugModel._base_manager = previous
        self.assertEqual(obj.slug, "asset")

    def test_two_part_source_is_bounded_to_the_slug_field_length(self):
        obj = _SlugModel(slug="")
        obj.__dict__.update({"manufacturer": SimpleNamespace(name="M" * 100), "model": "M" * 200})
        previous = _SlugModel._base_manager
        _SlugModel._base_manager = _Manager([False])
        try:
            generate_unique_slug(obj, ["manufacturer__name", "model"])
        finally:
            _SlugModel._base_manager = previous
        self.assertEqual(obj.slug, "m" * 100 + "-" + "m" * 154)
        self.assertLessEqual(len(obj.slug), _SlugModel._meta.get_field("slug").max_length)

    def test_collision_suffix_reserves_space_when_suffix_gains_a_digit(self):
        obj = _SlugModel(slug="", source="A" * 300)
        manager = _Manager([True] * 10 + [False])
        previous = _SlugModel._base_manager
        _SlugModel._base_manager = manager
        try:
            generate_unique_slug(obj, "slug_source")
        finally:
            _SlugModel._base_manager = previous
        self.assertEqual(obj.slug, "a" * 252 + "-10")
        self.assertTrue(all(len(call["slug"]) <= 255 for call in manager.filter_calls))


class _GroupManager:
    def __init__(self, children_by_parent, *, deleted_ids=()):
        self._children = children_by_parent
        self._deleted_ids = set(deleted_ids)
        self.filter_calls = []
        self.query_count = 0
        self._pending = []

    def filter(self, **kwargs):
        self.query_count += 1
        self.filter_calls.append(kwargs)
        if "pk" in kwargs:
            all_ids = {group_id for group_id, children in self._children.items() if group_id != "__live__"}
            all_ids.update(child for children in self._children.values() for child in children)
            self._pending = [kwargs["pk"]] if kwargs["pk"] in all_ids else []
            if kwargs.get("deleted_at__isnull") is True:
                self._pending = [child for child in self._pending if child not in self._deleted_ids]
            return self
        parents = kwargs.get("parent_id__in", [])
        children = {child for parent in parents for child in self._children.get(parent, ())}
        if kwargs.get("deleted_at__isnull") is True:
            children -= self._deleted_ids
        self._pending = sorted(children)
        return self

    def exclude(self, **kwargs):
        excluded = kwargs.get("pk__in", set())
        self._pending = [child for child in self._pending if child not in excluded]
        return self

    def exists(self):
        return bool(self._pending)

    def values_list(self, *args, **kwargs):
        return list(self._pending)


class _GroupModel:
    _base_manager = None


class _AncestorManager:
    def __init__(self, parent_by_id, *, deleted_ids=()):
        self._parent_by_id = parent_by_id
        self._deleted_ids = set(deleted_ids)
        self.filter_calls = []
        self._visible_ids = set(parent_by_id)

    def all(self):
        self._visible_ids = set(self._parent_by_id)
        return self

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        if kwargs.get("deleted_at__isnull") is True:
            self._visible_ids -= self._deleted_ids
        return self

    def values_list(self, *args, **kwargs):
        return [
            (group_id, parent_id) for group_id, parent_id in self._parent_by_id.items() if group_id in self._visible_ids
        ]


class TenantScopeContractTests(SimpleTestCase):
    def test_invalidation_of_unidentified_user_is_a_noop(self):
        invalidate_user_authorization_cache(None)

    def test_descendant_and_ancestor_walk_reject_missing_group(self):
        self.assertEqual(tenant_scope_module.get_descendant_tenant_group_ids(None), set())
        self.assertEqual(tenant_scope_module.get_ancestor_tenant_group_ids(None), set())

    def test_unregistered_provider_name_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "is not registered"):
            tenant_scope_module._call("__never_registered__", object())

    def test_live_only_walk_prunes_deleted_groups(self):
        manager = _GroupManager(
            {1: (2, 3), 2: (4,), 3: (), 4: ()},
            deleted_ids={2, 4},
        )
        _GroupModel._base_manager = manager
        with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
            ids = tenant_scope_module.get_descendant_tenant_group_ids(1, live_only=True)
        self.assertEqual(ids, {1, 3})
        self.assertTrue(all(call.get("deleted_at__isnull") is True for call in manager.filter_calls))

        deleted_root_manager = _GroupManager({5: (6,), 6: ()}, deleted_ids={5})
        _GroupModel._base_manager = deleted_root_manager
        with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
            self.assertEqual(tenant_scope_module.get_descendant_tenant_group_ids(5, live_only=True), set())
        self.assertEqual(deleted_root_manager.filter_calls, [{"pk": 5, "deleted_at__isnull": True}])

    def test_descendant_walk_collects_subtree_and_is_request_local_cached(self):
        manager = _GroupManager({101: (102, 103), 102: (104,), 103: (), 104: ()})
        _GroupModel._base_manager = manager
        cache_token = tenant_scope_module._descendant_group_ids_cache.set({})
        try:
            with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
                first = tenant_scope_module.get_descendant_tenant_group_ids(101)
                first_query_count = manager.query_count
                second = tenant_scope_module.get_descendant_tenant_group_ids(101)
        finally:
            tenant_scope_module._descendant_group_ids_cache.reset(cache_token)
        self.assertEqual(first, {101, 102, 103, 104})
        self.assertEqual(second, first)
        self.assertGreater(first_query_count, 0)
        self.assertEqual(manager.query_count, first_query_count)

    def test_ancestor_walk_returns_chain_and_fails_closed_on_cycle(self):
        _GroupModel._base_manager = _AncestorManager({1: None, 2: 1, 3: 2})
        with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
            self.assertEqual(tenant_scope_module.get_ancestor_tenant_group_ids(3), {1, 2, 3})
        live_manager = _AncestorManager({1: None, 2: 1, 3: 2}, deleted_ids={2})
        _GroupModel._base_manager = live_manager
        with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
            self.assertEqual(tenant_scope_module.get_ancestor_tenant_group_ids(3, live_only=True), set())
        self.assertEqual(live_manager.filter_calls, [{"deleted_at__isnull": True}])
        _GroupModel._base_manager = _AncestorManager({1: 2, 2: 1})
        with mock.patch.object(tenant_scope_module, "tenant_group_model", return_value=_GroupModel):
            self.assertEqual(tenant_scope_module.get_ancestor_tenant_group_ids(1), set())

    def test_provider_registration_resolves_through_owning_module(self):
        provider = ModuleType("kernel_test_provider")

        def provider_function(name, result):
            def function(*args, **kwargs):
                return (result, *args, *(() if "grants" not in kwargs else (kwargs["grants"],)))

            function.__module__ = provider.__name__
            function.__name__ = name
            setattr(provider, name, function)

        provider_function("accessible_tenant_ids_with_expiry", "expiry")
        provider_function("managed_accessible_tenant_ids", "managed")
        provider_function("applicable_grants", "grants")
        provider_function("resolve_effective_permissions_with_expiry", "permissions")
        provider_function("build_accessible_tenant_permissions_map", "map")
        previous = tenant_scope_module._provider_modules.copy()
        try:
            with mock.patch.dict(tenant_scope_module.sys.modules, {provider.__name__: provider}):
                tenant_scope_module.register_tenant_scope_provider(
                    accessible_tenant_ids_with_expiry=provider.accessible_tenant_ids_with_expiry,
                    managed_accessible_tenant_ids=provider.managed_accessible_tenant_ids,
                    applicable_grants=provider.applicable_grants,
                    resolve_effective_permissions_with_expiry=provider.resolve_effective_permissions_with_expiry,
                    build_accessible_tenant_permissions_map=provider.build_accessible_tenant_permissions_map,
                )
                self.assertEqual(tenant_scope_module.accessible_tenant_ids_with_expiry("u"), ("expiry", "u"))
                self.assertEqual(tenant_scope_module.managed_accessible_tenant_ids("u"), ("managed", "u"))
                self.assertEqual(tenant_scope_module.applicable_grants("u"), ("grants", "u"))
                self.assertEqual(
                    tenant_scope_module.resolve_effective_permissions_with_expiry("u", "t"),
                    ("permissions", "u", "t"),
                )
                self.assertEqual(
                    tenant_scope_module.build_accessible_tenant_permissions_map("u", grants=[1]),
                    ("map", "u", [1]),
                )
        finally:
            tenant_scope_module._provider_modules.clear()
            tenant_scope_module._provider_modules.update(previous)

    def test_model_lookup_helpers_use_the_django_registry(self):
        sentinel = object()
        with mock.patch.object(tenant_scope_module.apps, "get_model", return_value=sentinel) as get_model:
            self.assertIs(tenant_scope_module.tenant_group_model(), sentinel)
            self.assertIs(tenant_scope_module.tenant_model(), sentinel)
        self.assertEqual(
            get_model.call_args_list, [mock.call("organization", "TenantGroup"), mock.call("organization", "Tenant")]
        )

    def test_accessible_wrappers_delegate_to_registered_provider(self):
        sentinel = object()
        with (
            mock.patch.object(tenant_scope_module, "_typed_accessible_tenant_ids", return_value=sentinel) as typed,
            mock.patch.object(tenant_scope_module, "_call", return_value=sentinel) as call,
        ):
            self.assertIs(tenant_scope_module.accessible_tenant_ids("u"), sentinel)
            self.assertIs(tenant_scope_module.managed_accessible_tenant_ids("u"), sentinel)
        typed.assert_called_once_with("u")
        self.assertEqual(call.call_count, 1)


class KernelImportLeafTests(SimpleTestCase):
    def setUp(self):
        owner = type("Owner", (), {})
        owner.objects = _ImportRelatedManager(SimpleNamespace(pk=17))
        _ImportModel._meta = _ImportMeta(
            [
                _ImportField("id", primary_key=True),
                _ImportField("name"),
                _ImportField("slug", blank=True, null=True),
                _ImportField("owner", relation=True, related_model=owner),
            ]
        )
        _ImportModel.objects = _ImportRelatedManager()

    def test_import_context_and_form_metadata_are_safe(self):
        from core.importers.bulk_forms import _import_log_extra

        context = _import_log_extra(operation="row.persist", row_number=4, exception_type="ValueError")
        self.assertEqual(context["import_context"]["operation"], "row.persist")
        self.assertEqual(context["import_context"]["row_number"], 4)
        self.assertEqual(context["import_context"]["exception_type"], "ValueError")

        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            optional_fields = ["slug"]

        form = ImportForm()
        self.assertEqual(form.field_names, ["name", "slug"])
        form.cleaned_data = {}
        self.assertEqual(form.clean_csv_file(), None)

        minimal = _import_log_extra(operation="task.run")
        self.assertNotIn("row_number", minimal["import_context"])
        self.assertNotIn("exception_type", minimal["import_context"])

    def test_editor_csv_and_yaml_inputs_normalize_rows(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            optional_fields = ["enabled", "count", "note"]

        csv_form = ImportForm(
            data={
                "active_tab": "editor",
                "import_format": "csv",
                "delimiter": ",",
                "import_text": "name,enabled\n Asset , true \n",
            }
        )
        self.assertTrue(csv_form.is_valid(), csv_form.errors)
        self.assertEqual(csv_form._rows_data, [{"name": "Asset", "enabled": "true"}])

        yaml_form = ImportForm(
            data={
                "active_tab": "editor",
                "import_format": "yaml",
                "import_text": "- name: Asset\n  enabled: true\n  count: 3\n  note:\n- null: ignored\n",
            }
        )
        self.assertTrue(yaml_form.is_valid(), yaml_form.errors)
        self.assertEqual(yaml_form._rows_data[0], {"name": "Asset", "enabled": "True", "count": "3", "note": ""})
        self.assertEqual(yaml_form._rows_data[1], {})

        single_yaml_form = ImportForm(data={"import_format": "yaml", "import_text": "name: Single\ncount: 2\n"})
        self.assertTrue(single_yaml_form.is_valid(), single_yaml_form.errors)
        self.assertEqual(single_yaml_form._rows_data, [{"name": "Single", "count": "2"}])

    def test_import_parser_rejects_empty_missing_and_malformed_inputs(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]

        cases = [
            ({"active_tab": "editor", "import_text": ""}, "editor tab"),
            ({"active_tab": "editor", "import_text": "name\n"}, "CSV data is empty"),
            ({"active_tab": "editor", "import_text": "other\nvalue"}, "Missing required columns"),
            ({"active_tab": "editor", "import_format": "yaml", "import_text": "- item\n"}, "mappings"),
            ({"active_tab": "editor", "import_format": "yaml", "import_text": "value"}, "list of objects"),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected):
                form = ImportForm(data=data)
                self.assertFalse(form.is_valid())
                self.assertIn(expected, str(form.errors))

        malformed = ImportForm(data={"active_tab": "editor", "import_text": "name\nvalue"})
        with mock.patch("core.importers.bulk_forms.csv.DictReader", side_effect=csv.Error("bad")):
            self.assertFalse(malformed.is_valid())
        self.assertIn("Failed to parse CSV data", str(malformed.errors))

    def test_upload_parser_accepts_utf8_and_latin1_files(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]

        utf8_form = ImportForm(
            data={"active_tab": "upload"},
            files={"csv_file": SimpleUploadedFile("names.csv", b"\xef\xbb\xbfname\nAsset\n")},
        )
        self.assertTrue(utf8_form.is_valid(), utf8_form.errors)
        self.assertEqual(utf8_form._rows_data, [{"name": "Asset"}])

        latin1_form = ImportForm(
            data={"active_tab": "upload"},
            files={"csv_file": SimpleUploadedFile("names.csv", b"name\ncaf\xe9\n")},
        )
        self.assertTrue(latin1_form.is_valid(), latin1_form.errors)
        self.assertEqual(latin1_form._rows_data, [{"name": "café"}])

    def test_import_row_mapping_resolves_relations_and_skips_unknown_columns(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            optional_fields = ["owner", "not_a_field"]

        form = ImportForm()
        mapped = form.map_row({"id": "7", "name": " Asset ", "owner": "17", "not_a_field": "ignored"})
        # The pk column is never mapped: updates match on the declared natural key only.
        self.assertEqual(mapped, {"name": "Asset", "owner_id": 17})

    def test_import_row_create_update_and_validation_paths(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            update_key = ("name",)

        form = ImportForm()
        form.actor = mock.Mock(has_perm=mock.Mock(return_value=True))

        created = _ImportModel()
        form._create_instance = mock.Mock(return_value=created)
        _ImportModel.objects = _ImportKeyManager()
        form._import_row({"name": "Asset"}, 2)
        self.assertTrue(created.cleaned)
        self.assertTrue(created.saved)

        updated = _ImportModel()
        _ImportModel.objects = _ImportKeyManager(updated)
        form._import_row({"name": "Updated", "slug": "ignored-not-declared"}, 3)
        self.assertTrue(updated.snapshotted)
        self.assertTrue(updated.saved)
        form.actor.has_perm.assert_called_once()
        self.assertEqual(updated.name, "Updated")

        with self.assertRaises(ValidationError):
            form._validate_row({}, 4)
        with self.assertRaises(NotImplementedError):
            BulkImportForm().import_data()

    def test_update_requires_a_permitted_actor(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            update_key = ("name",)

        existing = _ImportModel(name="Original")
        _ImportModel.objects = _ImportKeyManager(existing)

        anonymous = ImportForm()
        with self.assertRaisesRegex(ValidationError, "permission to update"):
            anonymous._import_row({"name": "Original"}, 2)

        denied = ImportForm()
        denied.actor = mock.Mock(has_perm=mock.Mock(return_value=False))
        with self.assertRaisesRegex(ValidationError, "permission to update"):
            denied._import_row({"name": "Original"}, 3)
        self.assertFalse(existing.saved)
        self.assertFalse(existing.snapshotted)

    def test_ambiguous_update_key_is_rejected(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]
            update_key = ("name",)

        _ImportModel.objects = _ImportKeyManager(_ImportModel(name="a"), _ImportModel(name="a"))
        form = ImportForm()
        form.actor = mock.Mock(has_perm=mock.Mock(return_value=True))
        with self.assertRaisesRegex(ValidationError, "More than one"):
            form._import_row({"name": "a"}, 2)

    def test_form_without_update_key_never_updates(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name"]

        existing = _ImportModel(name="Original")
        _ImportModel.objects = _ImportKeyManager(existing)
        form = ImportForm()
        created = _ImportModel()
        form._create_instance = mock.Mock(return_value=created)
        form._import_row({"id": "1", "name": "Original"}, 2)
        self.assertTrue(created.saved)
        self.assertFalse(existing.saved)

    def test_required_foreign_key_is_satisfied_by_its_mapped_attname(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel
            required_fields = ["name", "owner"]

        form = ImportForm()
        form._validate_row({"name": "x", "owner_id": 17}, 2)
        with self.assertRaisesRegex(ValidationError, '"owner" is required'):
            form._validate_row({"name": "x"}, 3)

    def test_import_data_collects_validation_and_unexpected_row_errors(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel

        form = ImportForm()
        form._rows_data = [{"name": "ok"}, {"name": "bad"}, {"name": "broken"}]
        form._import_row = mock.Mock(side_effect=[None, ValidationError("invalid"), RuntimeError("internal")])
        atomic = mock.MagicMock()
        with mock.patch("core.importers.bulk_forms.transaction.atomic", return_value=atomic):
            result = form.import_data()
        self.assertEqual(result.imported_count, 1)
        self.assertEqual(result.errors[0], "Row 3: invalid")
        self.assertEqual(result.errors[1], "Row 4: could not be imported due to an unexpected error.")

        empty = ImportForm()
        self.assertEqual(empty.import_data().imported_count, 0)

    def test_import_row_handles_plain_instances(self):
        class ImportForm(BulkImportForm):
            model = _ImportModel

        form = ImportForm()

        class PlainInstance:
            def __init__(self):
                self.saved = False

            def save(self):
                self.saved = True

        plain = PlainInstance()
        form._create_instance = mock.Mock(return_value=plain)
        form._import_row({"name": "plain"}, 2)
        self.assertTrue(plain.saved)

    def test_import_form_registration_without_model_is_a_noop(self):
        from core.importers import bulk_forms

        previous = bulk_forms._IMPORT_FORM_REGISTRY.copy()
        try:

            class UncuratedForm(BulkImportForm):
                pass

            self.assertIs(register_import_form(UncuratedForm), UncuratedForm)
            self.assertNotIn(UncuratedForm, bulk_forms._IMPORT_FORM_REGISTRY)
        finally:
            bulk_forms._IMPORT_FORM_REGISTRY.clear()
            bulk_forms._IMPORT_FORM_REGISTRY.update(previous)

    def test_import_model_policy_is_an_allowlist(self):
        from django.apps import apps

        self.assertFalse(is_model_importable(None))
        self.assertTrue(is_model_importable(apps.get_model("assets", "asset")))
        for label in ("users.user", "assets.assetassignment", "compliance.custodyreceipt", "organization.tenant"):
            with self.subTest(label=label):
                self.assertFalse(is_model_importable(apps.get_model(label)))

    def test_undeclared_model_has_no_import_form(self):
        from core.importers.bulk_forms import ImportNotAllowed

        with self.assertRaises(ImportNotAllowed):
            get_import_form_class(_ImportModel)

    def test_registered_form_for_an_undeclared_model_is_still_denied(self):
        from core.importers import bulk_forms

        model = _ImportModel
        previous = bulk_forms._IMPORT_FORM_REGISTRY.copy()
        try:

            class CuratedForm(BulkImportForm):
                pass

            CuratedForm.model = model
            register_import_form(CuratedForm)
            self.assertFalse(is_model_importable(model))
        finally:
            bulk_forms._IMPORT_FORM_REGISTRY.clear()
            bulk_forms._IMPORT_FORM_REGISTRY.update(previous)

    def test_registered_import_form_is_returned_for_a_declared_model(self):
        from django.apps import apps

        form = get_import_form_class(apps.get_model("assets", "manufacturer"))
        self.assertEqual(form.model._meta.label_lower, "assets.manufacturer")

    def test_related_resolution_checks_id_exact_case_insensitive_and_fallback(self):
        class RelatedMeta:
            def get_field(self, name):
                if name not in {"slug", "name"}:
                    raise FieldDoesNotExist(name)
                return object()

        class RelatedObjects:
            def __init__(self):
                self.calls = []

            def filter(self, **kwargs):
                self.calls.append(kwargs)
                return self

            def first(self):
                query = self.calls[-1]
                if query == {"pk": 7} or query == {"slug": "Asset"} or query == {"slug__iexact": "ASSET"}:
                    return SimpleNamespace(pk=99)
                return None

        related_model = SimpleNamespace(_meta=RelatedMeta(), objects=RelatedObjects())
        self.assertEqual(resolve_related(related_model, "7"), 99)
        self.assertEqual(resolve_related(related_model, "Asset"), 99)
        self.assertEqual(resolve_related(related_model, "ASSET"), 99)
        with self.assertRaises(ValidationError):
            resolve_related(related_model, "unknown")

    def test_slug_source_tuple_and_empty_source_have_safe_fallbacks(self):
        obj = _SlugModel(slug="")
        obj.parent = SimpleNamespace(name="Parent")
        obj.code = "Child"
        previous = _SlugModel._base_manager
        _SlugModel._base_manager = _Manager([False, False])
        try:
            generate_unique_slug(obj, ["parent__name", "code"])
            self.assertEqual(obj.slug, "parent-child")
            obj.slug = ""
            obj.parent = None
            obj.code = ""
            generate_unique_slug(obj, ["parent__name", "code"])
            self.assertEqual(obj.slug, "auto-slug")
        finally:
            if previous is None:
                _SlugModel._base_manager = previous
            else:
                _SlugModel._base_manager = previous
