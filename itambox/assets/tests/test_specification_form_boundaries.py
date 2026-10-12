import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.http import QueryDict
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from assets.forms import AssetForm, AssetTypeForm
from assets.forms.assettype_form import (
    _DRAFT_PREFIX,
    _T15_UNSET,
    _build_t15_custom_field,
    _build_t15_presence_field,
    _choice_options,
    _coerce_t15_boolean,
    _display_t15_value,
    _history_entries,
)
from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from assets.services.specifications.commands import set_asset_type_composition
from assets.tests.test_asset_form_value_preservation import _minimal_asset_form_data
from core.tests.mixins import TenantTestMixin
from extras.models import CustomField, CustomFieldset


class SpecificationFormBoundaryTests(TenantTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.setup_tenant_context(
            name="Form command boundary",
            slug="form-command-boundary",
            permissions=["assets.add_asset", "assets.view_asset", "assets.change_asset"],
        )
        manufacturer = Manufacturer.objects.create(name="Form Boundary", slug="form-boundary")
        self.source = AssetType.objects.create(manufacturer=manufacturer, model="Source", slug="boundary-source")
        self.target = AssetType.objects.create(manufacturer=manufacturer, model="Target", slug="boundary-target")
        self.status = StatusLabel.objects.create(
            name="Boundary available", slug="boundary-available", type="deployable"
        )
        with self.tenant_context(self.tenant):
            self.asset = Asset.objects.create(
                name="Form boundary asset",
                asset_tag="FORM-BOUNDARY-ASSET",
                asset_type=self.source,
                status=self.status,
                tenant=self.tenant,
            )
        self.request = RequestFactory().post("/assets/")
        self.request.user = self.tenant_user
        self.request.tenant = self.tenant

    def _asset_type_revisions(self, instance, data=None):
        if data is None:
            preview = AssetTypeForm(instance=instance, request=self.request)
            return {
                "expected_resource_revision": preview.fields["expected_resource_revision"].initial,
                "expected_definition_revision": preview.fields["expected_definition_revision"].initial,
            }
        preview_data = dict(data)
        preview_data["_reload"] = "1"
        preview_request = RequestFactory().post("/asset-types/", preview_data, HTTP_HX_REQUEST="true")
        preview_request.user = self.tenant_admin
        preview = AssetTypeForm(data=preview_data, instance=instance, request=preview_request)
        return {
            "expected_resource_revision": preview.data["expected_resource_revision"],
            "expected_definition_revision": preview.data["expected_definition_revision"],
        }

    def test_http_asset_creation_reaches_native_form_with_authenticated_actor(self):
        self.client_login_to_tenant(self.tenant_admin, self.tenant)
        data = _minimal_asset_form_data(
            self.asset,
            self.status,
            self.source,
            tenant=self.tenant.pk,
            name="HTTP form created",
            asset_tag="HTTP-FORM-CREATED",
        )
        response = self.client.post(reverse("assets:asset_create"), data, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 302, response.content[:2000])
        created = Asset._base_manager.get(asset_tag="HTTP-FORM-CREATED")
        self.assertEqual(created.tenant_id, self.tenant.pk)
        self.assertEqual(created.asset_type_id, self.source.pk)

    def test_mixed_form_updates_lock_catalogue_before_native_owner_write(self):
        admin_request = RequestFactory().post("/asset-types/")
        admin_request.user = self.tenant_admin
        type_data = {
            "manufacturer": self.source.manufacturer_id,
            "model": "Changed native model",
            "slug": self.source.slug,
        }
        type_data.update(self._asset_type_revisions(self.source))
        with self.tenant_context(self.tenant):
            forms = (
                (
                    AssetForm(
                        data=_minimal_asset_form_data(
                            self.asset, self.status, self.target, tenant=self.tenant.pk, name="Changed native name"
                        ),
                        instance=self.asset,
                        request=self.request,
                    ),
                    "assets_asset",
                ),
                (
                    AssetTypeForm(
                        data=type_data,
                        instance=self.source,
                        request=admin_request,
                    ),
                    "assets_assettype",
                ),
            )
            for form, table in forms:
                with self.subTest(form=type(form).__name__):
                    self.assertTrue(form.is_valid(), form.errors.as_json())
                    with CaptureQueriesContext(connection) as queries:
                        form.save()
                    sql = [entry["sql"] for entry in queries.captured_queries]
                    locks = [i for i, query in enumerate(sql) if "pg_advisory_xact_lock" in query]
                    writes = [
                        i
                        for i, query in enumerate(sql)
                        if query.startswith(f"UPDATE {connection.ops.quote_name(table)}")
                    ]
                    self.assertTrue(locks, sql)
                    self.assertTrue(writes, sql)
                    self.assertLess(locks[0], writes[0], "Form wrote native fields before catalogue locking")

    def test_deferred_type_m2m_does_not_mutate_composition_before_command(self):
        fieldset = CustomFieldset.objects.create(
            namespace="local", slug="deferred-composition", label="Deferred composition"
        )
        request = RequestFactory().post("/asset-types/")
        request.user = self.tenant_admin
        form_data = {
            "manufacturer": self.source.manufacturer_id,
            "model": self.source.model,
            "slug": self.source.slug,
            "custom_fieldsets": [fieldset.pk],
        }
        form_data.update(self._asset_type_revisions(self.source, form_data))
        form = AssetTypeForm(
            data=form_data,
            instance=self.source,
            request=request,
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        pending = form.save(commit=False)
        pending.save()

        def observe_real_command(**kwargs):
            self.assertFalse(self.source.fieldset_memberships.exists(), "Native save_m2m bypassed composition command")
            return set_asset_type_composition(**kwargs)

        with patch(
            "assets.forms.assettype_form.set_asset_type_composition", side_effect=observe_real_command
        ) as command:
            form.save_m2m()
        command.assert_called_once()
        self.assertEqual(list(self.source.fieldset_memberships.values_list("fieldset_id", flat=True)), [fieldset.pk])

    def test_asset_form_status_edit_persists_native_disposal_side_effects(self):
        archived = StatusLabel.objects.create(name="Form archived", slug="form-boundary-archived", type="archived")
        previous_timestamp = self.asset.updated_at
        with self.tenant_context(self.tenant):
            form = AssetForm(
                data=_minimal_asset_form_data(self.asset, archived, self.source, tenant=self.tenant.pk),
                instance=self.asset,
                request=self.request,
            )
            self.assertTrue(form.is_valid(), form.errors.as_json())
            saved = form.save()
            saved.refresh_from_db()
            self.assertIsNotNone(saved.disposed_at)
            self.assertIsNotNone(saved.disposal_value)
            self.assertNotEqual(saved.updated_at, previous_timestamp)

    def test_type_form_native_edit_advances_timestamp(self):
        request = RequestFactory().post("/asset-types/")
        request.user = self.tenant_admin
        previous_timestamp = self.source.updated_at
        form_data = {
            "manufacturer": self.source.manufacturer_id,
            "model": "Updated native Type",
            "slug": self.source.slug,
        }
        form_data.update(self._asset_type_revisions(self.source))
        form = AssetTypeForm(
            data=form_data,
            instance=self.source,
            request=request,
        )
        self.assertTrue(form.is_valid(), form.errors.as_json())
        saved = form.save()
        saved.refresh_from_db()
        self.assertNotEqual(saved.updated_at, previous_timestamp)

    def test_native_asset_form_create_does_not_require_change_permission(self):
        self.tenant_role.permissions = ["assets.add_asset", "assets.view_asset"]
        self.tenant_role.save(update_fields=["permissions"])
        unsaved = Asset(name="Native form creation", asset_tag="FORM-NATIVE-CREATE")
        with self.tenant_context(self.tenant):
            form = AssetForm(
                data=_minimal_asset_form_data(unsaved, self.status, self.source, tenant=self.tenant.pk),
                request=self.request,
            )
            self.assertTrue(form.is_valid(), form.errors)
            with patch("assets.forms.asset_form.update_asset_specifications") as command:
                created = form.save()
            command.assert_not_called()
        created.refresh_from_db()
        self.assertEqual(created.tenant_id, self.tenant.pk)
        self.assertEqual(created.asset_type_id, self.source.pk)
        self.assertEqual(created.custom_field_data, {})

    def test_commit_false_does_not_expose_type_switch_to_native_save(self):
        with self.tenant_context(self.tenant):
            form = AssetForm(
                data=_minimal_asset_form_data(self.asset, self.status, self.target, tenant=self.tenant.pk),
                instance=self.asset,
                request=self.request,
            )
            self.assertTrue(form.is_valid(), form.errors.as_json())
            pending = form.save(commit=False)
            self.assertEqual(pending.asset_type_id, self.source.pk)
            pending.save()
            self.asset.refresh_from_db()
            self.assertEqual(self.asset.asset_type_id, self.source.pk)
            form.save_m2m()
            self.asset.refresh_from_db()
            self.assertEqual(self.asset.asset_type_id, self.target.pk)


def _t15_definition(name, field_type, **overrides):
    values = dict(
        name=name,
        field_type=field_type,
        label=name,
        help_text="",
        required=False,
        nullable=False,
        lifecycle="active",
        choice_set=None,
        decimal_scale=2,
        text_max_length=None,
        regex=None,
        validation_rule=None,
        minimum_value=None,
        maximum_value=None,
        max_values=None,
        choice_set_id=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _t15_choice(key, label, position, lifecycle="active"):
    return SimpleNamespace(key=key, label=label, position=position, lifecycle=lifecycle)


def _t15_choices(*rows, lifecycle="active"):
    return SimpleNamespace(
        lifecycle=lifecycle,
        choices=tuple(rows),
        _prefetched_objects_cache={"choices": list(rows)},
    )


class AssetTypeFormT15HelperTests(SimpleTestCase):
    def test_choice_options_keep_active_rows_and_stored_deprecated_rows(self):
        definition = SimpleNamespace(
            choice_set=_t15_choices(
                _t15_choice("a", "Alpha", 1),
                _t15_choice("b", "Beta", 2, lifecycle="deprecated"),
                _t15_choice("c", "Gamma", 3, lifecycle="deprecated"),
            )
        )

        self.assertEqual(_choice_options(definition, ["c"]), [("a", "Alpha"), ("c", "Gamma (No longer offered)")])

    def test_choice_options_read_a_related_manager_when_present(self):
        class _Manager:
            def all(self):
                return [_t15_choice("z", "Zulu", 1)]

        definition = SimpleNamespace(choice_set=SimpleNamespace(choices=_Manager()))

        self.assertEqual(_choice_options(definition, None), [("z", "Zulu")])

    def test_boolean_coercion_uses_the_unset_and_null_sentinels(self):
        self.assertIs(_coerce_t15_boolean(""), _T15_UNSET)
        self.assertIsNone(_coerce_t15_boolean("__null__"))
        self.assertIs(_coerce_t15_boolean("TRUE"), True)
        self.assertIs(_coerce_t15_boolean("false"), False)

    def test_nullable_boolean_field_offers_an_explicit_null_choice(self):
        definition = _t15_definition("flag", CustomField.FIELD_TYPE_BOOLEAN, required=True, nullable=True)

        field = _build_t15_custom_field(definition, None, has_stored_value=True)

        self.assertEqual([key for key, _ in field.choices], ["", "true", "false", "__null__"])
        self.assertEqual(field.initial, "__null__")
        self.assertIs(_coerce_t15_boolean(field.initial), None)
        self.assertEqual(_build_t15_custom_field(definition, False, has_stored_value=True).initial, "false")
        self.assertEqual(_build_t15_custom_field(definition, None, has_stored_value=False).initial, "")

    def test_plain_fields_carry_the_specification_input_attribute(self):
        field = _build_t15_custom_field(_t15_definition("note", CustomField.FIELD_TYPE_TEXT, text_max_length=32))

        self.assertEqual(field.widget.attrs["data-specification-input"], "1")

    def test_presence_field_variants_follow_the_field_type(self):
        text = _build_t15_presence_field(_t15_definition("note", CustomField.FIELD_TYPE_TEXT, nullable=True))
        integer = _build_t15_presence_field(_t15_definition("count", CustomField.FIELD_TYPE_INTEGER))

        self.assertEqual([key for key, _ in text.choices], ["", "value", "empty", "null"])
        self.assertEqual([key for key, _ in integer.choices], ["", "value"])

    def test_display_value_renders_null_boolean_and_choice_values(self):
        boolean = _t15_definition("flag", CustomField.FIELD_TYPE_BOOLEAN)
        collection = SimpleNamespace(
            field_type=CustomField.FIELD_TYPE_MULTI_SELECT,
            choice_set=_t15_choices(_t15_choice("a", "Alpha", 1), _t15_choice("b", "Beta", 2)),
        )

        self.assertEqual(_display_t15_value(boolean, None), "null")
        self.assertEqual(_display_t15_value(boolean, True), "Yes")
        self.assertEqual(_display_t15_value(boolean, False), "No")
        self.assertEqual(_display_t15_value(collection, ["a", "b"]), "Alpha, Beta")
        self.assertEqual(_display_t15_value(collection, "a"), "Alpha")
        self.assertEqual(_display_t15_value(collection, "zz"), "zz")

    def test_history_entries_flag_deprecated_fields(self):
        definition = _t15_definition(
            "kind",
            CustomField.FIELD_TYPE_SINGLE_SELECT,
            lifecycle="deprecated",
            choice_set_id=1,
            choice_set=_t15_choices(_t15_choice("a", "Alpha", 1)),
        )

        with patch.object(CustomField, "objects") as manager:
            manager.filter.return_value.prefetch_related.return_value = [definition]
            entries = _history_entries({"kind": "a"}, {"cf_kind": definition})

        self.assertEqual(entries[0]["state"], "historical")
        self.assertEqual([str(reason) for reason in entries[0]["reasons"]], ["Deprecated field"])
        self.assertEqual(entries[0]["display_value"], "Alpha")

    def test_history_entries_flag_deprecated_choices_and_invalid_values(self):
        definition = _t15_definition(
            "kind",
            CustomField.FIELD_TYPE_SINGLE_SELECT,
            choice_set=_t15_choices(_t15_choice("a", "Alpha", 1), _t15_choice("old", "Old", 2, lifecycle="deprecated")),
        )
        boolean = _t15_definition("flag", CustomField.FIELD_TYPE_BOOLEAN)

        with patch.object(CustomField, "objects") as manager:
            manager.filter.return_value.prefetch_related.return_value = [definition, boolean]
            entries = _history_entries(
                {"kind": "old", "flag": "not-a-boolean"}, {"cf_kind": definition, "cf_flag": boolean}
            )

        by_key = {entry["key"]: entry for entry in entries}
        self.assertEqual(by_key["kind"]["state"], "invalid")
        self.assertEqual(
            [str(reason) for reason in by_key["kind"]["reasons"]], ["Deprecated choice", "Invalid stored value"]
        )
        self.assertEqual(by_key["flag"]["state"], "invalid")
        self.assertEqual([str(reason) for reason in by_key["flag"]["reasons"]], ["Invalid stored value"])


class AssetTypeFormT15StateTests(SimpleTestCase):
    def _form(self, **attrs):
        form = AssetTypeForm.__new__(AssetTypeForm)
        form.is_bound = attrs.pop("is_bound", True)
        for key, value in attrs.items():
            setattr(form, key, value)
        return form

    def test_omitted_fieldset_presence_expands_to_category_defaults(self):
        form = self._form(_category_default_fieldset_ids=lambda category_id: [5, 7])
        data = QueryDict("specification_fieldsets_presence=omitted&category=3", mutable=True)
        kwargs = {"data": data}

        form._normalize_omitted_fieldset_data(kwargs)

        self.assertEqual(kwargs["data"].getlist("custom_fieldsets"), ["5", "7"])
        self.assertEqual(data.getlist("custom_fieldsets"), [])

    def test_explicit_or_missing_fieldset_data_is_left_untouched(self):
        form = self._form(_category_default_fieldset_ids=lambda category_id: [5])
        explicit = QueryDict("specification_fieldsets_presence=explicit", mutable=True)
        kwargs = {"data": explicit}

        form._normalize_omitted_fieldset_data(kwargs)
        self.assertIs(kwargs["data"], explicit)

        empty = {}
        form._normalize_omitted_fieldset_data(empty)
        self.assertEqual(empty, {})

    def test_raw_selected_ids_use_category_defaults_for_omitted_presence(self):
        form = self._form(
            data={"specification_fieldsets_presence": "omitted", "category": "9"},
            _category_default_fieldset_ids=lambda category_id: [int(category_id)],
        )

        self.assertEqual(form._calculate_raw_selected_fieldset_ids(), [9])

    def test_raw_selected_ids_accept_scalar_and_collection_payloads(self):
        scalar = self._form(data={"custom_fieldsets": "4"})
        collection = self._form(data={"custom_fieldsets": ["4", "x", 6]})

        self.assertEqual(scalar._calculate_raw_selected_fieldset_ids(), [4])
        self.assertEqual(collection._calculate_raw_selected_fieldset_ids(), [4, 6])

    def test_raw_selected_ids_come_from_the_instance_when_unbound(self):
        memberships = SimpleNamespace(
            order_by=lambda position: SimpleNamespace(values_list=lambda *args, **kwargs: ["3", "5"])
        )
        form = self._form(is_bound=False, instance=SimpleNamespace(pk=7, fieldset_memberships=memberships))

        self.assertEqual(form._calculate_raw_selected_fieldset_ids(), ["3", "5"])

    def test_raw_selected_ids_fall_back_to_the_draft_category_and_initial_values(self):
        defaults = self._form(
            is_bound=False,
            instance=SimpleNamespace(pk=None),
            _custom_fieldsets_explicit=False,
            _draft_category=SimpleNamespace(pk=8),
            _category_default_fieldset_ids=lambda category_id: [category_id],
        )
        from_query = self._form(
            is_bound=False,
            instance=SimpleNamespace(pk=None),
            _custom_fieldsets_explicit=True,
            initial={"custom_fieldsets": SimpleNamespace(values_list=lambda *args, **kwargs: [1, 2])},
        )
        plain = self._form(
            is_bound=False,
            instance=SimpleNamespace(pk=None),
            _custom_fieldsets_explicit=True,
            initial={"custom_fieldsets": [SimpleNamespace(pk=3), 4]},
        )

        self.assertEqual(defaults._calculate_raw_selected_fieldset_ids(), [8])
        self.assertEqual(from_query._calculate_raw_selected_fieldset_ids(), [1, 2])
        self.assertEqual(plain._calculate_raw_selected_fieldset_ids(), [3, 4])

    def test_draft_transport_parses_json_payloads_and_skips_broken_ones(self):
        data = QueryDict("", mutable=True)
        data[f"{_DRAFT_PREFIX}note"] = json.dumps("A")
        data[f"{_DRAFT_PREFIX}broken"] = "{not json"

        form = self._form(data=data)

        self.assertEqual(form._read_t15_draft_transport(), {"note": "A"})
        self.assertEqual(self._form(is_bound=False, data=data)._read_t15_draft_transport(), {})

    def test_injected_drafts_only_fill_absent_known_keys(self):
        data = QueryDict("cf_note=explicit", mutable=True)
        form = self._form(
            data=data,
            custom_field_definitions={
                "cf_note": _t15_definition("note", CustomField.FIELD_TYPE_TEXT),
                "cf_count": _t15_definition("count", CustomField.FIELD_TYPE_INTEGER),
                "cf_ports": _t15_definition("ports", CustomField.FIELD_TYPE_MULTI_SELECT),
            },
            fields={"cf_ports__presence": SimpleNamespace()},
        )

        form._inject_t15_drafts({"note": "ignored", "count": None, "ports": ["a", "b"], "unknown": "x"})

        self.assertEqual(form.data.get("cf_note"), "explicit")
        self.assertEqual(form.data.get("cf_count"), "")
        self.assertEqual(form.data.getlist("cf_ports"), ["a", "b"])
        self.assertEqual(form.data.get("cf_ports__presence"), "value")

    def test_injected_drafts_are_skipped_when_unbound_or_empty(self):
        data = QueryDict("x=1", mutable=True)
        unbound = self._form(is_bound=False, data=data, custom_field_definitions={}, fields={})
        unbound._inject_t15_drafts({"note": "A"})
        self.assertIs(unbound.data, data)

        empty = self._form(data=data, custom_field_definitions={}, fields={})
        empty._inject_t15_drafts({})
        self.assertIs(empty.data, data)

    def test_presence_modes_map_to_empty_null_and_removal(self):
        data = QueryDict("cf_ports__presence=empty&cf_note__presence=null&cf_count__presence=", mutable=True)
        form = self._form(
            data=data,
            custom_field_definitions={
                "cf_ports": _t15_definition("ports", CustomField.FIELD_TYPE_MULTI_SELECT),
                "cf_note": _t15_definition("note", CustomField.FIELD_TYPE_TEXT),
                "cf_count": _t15_definition("count", CustomField.FIELD_TYPE_INTEGER),
            },
            custom_field_presence_keys={
                "cf_ports": "cf_ports__presence",
                "cf_note": "cf_note__presence",
                "cf_count": "cf_count__presence",
            },
            custom_field_clear_keys={},
        )
        cleaned = {
            "cf_ports": _T15_UNSET,
            "cf_note": _T15_UNSET,
            "cf_count": "7",
            "cf_ports__presence": "empty",
            "cf_note__presence": "null",
            "cf_count__presence": "",
        }

        form._apply_t15_presence(cleaned)

        self.assertEqual(cleaned["cf_ports"], [])
        self.assertIsNone(cleaned["cf_note"])
        self.assertNotIn("cf_count", cleaned)

    def test_presence_conflict_with_explicit_removal_is_reported(self):
        errors = []
        form = self._form(
            data=QueryDict("cf_note__presence=value", mutable=True),
            custom_field_definitions={"cf_note": _t15_definition("note", CustomField.FIELD_TYPE_TEXT)},
            custom_field_presence_keys={"cf_note": "cf_note__presence"},
            custom_field_clear_keys={"cf_note": "cf_note__clear"},
        )
        form.add_error = lambda key, message: errors.append((key, str(message)))
        cleaned = {"cf_note": "A", "cf_note__presence": "value", "cf_note__clear": True}

        form._apply_t15_presence(cleaned)

        self.assertEqual(errors, [("cf_note__presence", "Choose either removal or an explicit value, not both.")])
        self.assertEqual(cleaned["cf_note"], "A")

    def test_omitted_presence_keys_drop_untouched_values(self):
        form = self._form(
            data=QueryDict("other=1", mutable=True),
            custom_field_definitions={"cf_note": _t15_definition("note", CustomField.FIELD_TYPE_TEXT)},
            custom_field_presence_keys={"cf_note": "cf_note__presence"},
            custom_field_clear_keys={"cf_note": "cf_note__clear"},
        )
        cleaned = {"cf_note": _T15_UNSET}

        form._apply_t15_presence(cleaned)

        self.assertNotIn("cf_note", cleaned)

    def test_fieldset_source_uses_the_management_kind(self):
        form = self._form()

        self.assertEqual(form._fieldset_source(SimpleNamespace(management_kind="core")), "Core")
