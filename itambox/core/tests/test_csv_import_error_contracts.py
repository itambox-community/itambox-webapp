import logging
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.test import RequestFactory, SimpleTestCase
from django.utils.translation import override

from assets.models import Manufacturer
from core.forms.import_forms import BulkImportForm, ImportResult
from core.importers.bulk_forms import _import_log_extra, _model_has_concrete_field
from core.models import Job, Notification
from core.tasks.csv_import import _task_log_extra, import_csv_task
from core.tests.mixins import TenantTestMixin
from itambox.views.generic import ObjectImportView

SECRET = "customer@example.test bearer-secret secret_idx_42"


class _ConditionalImportForm(BulkImportForm):
    model = Manufacturer
    required_fields = ["name"]
    optional_fields = ["slug"]

    def _create_instance(self, mapped_data):
        if mapped_data["name"] == "explode":
            instance = MagicMock()
            instance.full_clean.return_value = None
            instance.save.side_effect = IntegrityError(SECRET)
            return instance
        return super()._create_instance(mapped_data)


class _SyntheticColumnImportForm(_ConditionalImportForm):
    optional_fields = ["synthetic_column"]


@pytest.mark.django_db
class TestImportRowContract(TenantTestMixin):
    def test_curated_form_loader_failure_falls_back_without_leaking_exception(self, caplog, monkeypatch):
        """Issue #100: curated forms are registered in ``AppConfig.ready`` — the
        lazy discovery (and its failure path) is gone. An unregistered model falls
        back to the dynamic form path without raising and without leaking anything
        into the log."""
        from core.importers import bulk_forms

        with caplog.at_level(logging.ERROR):
            result = bulk_forms.get_registered_import_form(MagicMock())

        assert result is None
        assert not caplog.records

    def test_field_help_preserves_curated_non_model_columns(self):
        view = ObjectImportView()
        view.model_form = _SyntheticColumnImportForm

        assert view._get_fields_info() == [
            {
                "name": "name",
                "required": True,
                "accessor": "",
                "description": "Name",
                "choices": [],
            },
            {
                "name": "synthetic_column",
                "required": False,
                "accessor": "",
                "description": "",
                "choices": [],
            },
        ]

    def test_log_context_includes_supplied_optional_fields(self):
        extra = _import_log_extra(operation="row.persist", row_number=9, exception_type="RuntimeError")

        assert extra["import_context"]["row_number"] == 9
        assert extra["import_context"]["exception_type"] == "RuntimeError"

        minimal = _import_log_extra(operation="row.persist")
        assert "row_number" not in minimal["import_context"]
        assert "exception_type" not in minimal["import_context"]

        task_minimal = _task_log_extra(operation="task.run", tenant_id=1, actor_id=2)
        assert "exception_type" not in task_minimal["import_context"]

    def test_non_model_field_is_not_treated_as_concrete(self):
        model = MagicMock()
        model._meta.get_field.side_effect = FieldDoesNotExist

        assert _model_has_concrete_field(model, "synthetic") is False

    def test_empty_row_set_returns_typed_result(self):
        result = _ConditionalImportForm().import_data()

        assert result == ImportResult(0, ["No data to import."])

    @pytest.mark.parametrize(
        ("import_format", "patched", "side_effect", "message"),
        [
            (
                "csv",
                "core.importers.bulk_forms.csv.DictReader",
                __import__("csv").Error(SECRET),
                "Failed to parse CSV data.",
            ),
            (
                "yaml",
                "core.importers.bulk_forms.yaml.safe_load",
                __import__("yaml").YAMLError(SECRET),
                "Failed to parse YAML data.",
            ),
        ],
    )
    def test_parser_errors_are_normalized(self, import_format, patched, side_effect, message):
        form = _ConditionalImportForm(
            data={"active_tab": "editor", "import_format": import_format, "import_text": "name: value"}
        )

        with patch(patched, side_effect=side_effect):
            assert not form.is_valid()

        assert message in str(form.non_field_errors())
        assert SECRET not in str(form.errors)

    def test_update_calls_snapshot_full_clean_and_save(self):
        instance = MagicMock()
        form = _ConditionalImportForm()
        form.actor = MagicMock()
        form.actor.has_perm.return_value = True
        form.map_row = MagicMock(return_value={"name": "updated"})
        form._validate_row = MagicMock()
        form._find_existing = MagicMock(return_value=instance)

        form._import_row({"name": "updated"}, 2)

        form.actor.has_perm.assert_called_once_with("assets.change_manufacturer", instance)
        instance.snapshot.assert_called_once_with()
        instance.full_clean.assert_called_once_with()
        instance.save.assert_called_once_with()

    def test_create_supports_plain_instances_without_optional_hooks(self):
        class PlainInstance:
            def __init__(self):
                self.saved = False

            def save(self):
                self.saved = True

        form = _ConditionalImportForm()
        instance = PlainInstance()
        form.map_row = MagicMock(return_value={"name": "plain"})
        form._validate_row = MagicMock()
        form._create_instance = MagicMock(return_value=instance)

        form._import_row({"name": "plain"}, 2)

        assert instance.saved is True

    def test_map_row_without_model_is_empty(self):
        form = BulkImportForm()

        assert form.map_row({"name": "ignored"}) == {}

    def test_unexpected_row_rolls_back_to_savepoint_and_later_rows_continue(self, caplog):
        self.setup_tenant_context(name="Import Tenant", slug="import-tenant")
        self.set_active_tenant(self.tenant, self.tenant_membership)
        form = _ConditionalImportForm()
        form._rows_data = [{"name": "explode", "slug": "explode"}, {"name": "safe manufacturer", "slug": "safe"}]

        with caplog.at_level(logging.ERROR), transaction.atomic():
            result = form.import_data()

        assert isinstance(result, ImportResult)
        assert result.imported_count == 1
        assert result.errors == ["Row 2: could not be imported due to an unexpected error."]
        assert Manufacturer.objects.filter(name="safe manufacturer").exists()
        record = next(record for record in caplog.records if hasattr(record, "import_context"))
        assert record.import_context["operation"] == "row.persist"
        assert record.import_context["row_number"] == 2
        assert record.import_context["exception_type"] == "IntegrityError"
        assert record.import_context["tenant_id"] == self.tenant.pk
        assert SECRET not in caplog.text


@pytest.mark.django_db
class TestImportTaskAbortContract(TenantTestMixin):
    @patch("core.tasks.csv_import.get_import_form_class")
    def test_abort_persists_and_logs_only_safe_contract_fields(self, get_import_form_class, caplog):
        self.setup_tenant_context(name="Task Tenant", slug="task-tenant")
        user = self.tenant_user
        job = Job.objects.create(name="Import Job", tenant=self.tenant, status=Job.STATUS_PENDING)
        form = MagicMock()
        form.import_data.side_effect = RuntimeError(SECRET)
        get_import_form_class.return_value = MagicMock(return_value=form)

        with caplog.at_level(logging.ERROR), override("en"):
            import_csv_task(
                job_id=job.pk,
                rows_data=[{"email": "customer@example.test"}],
                app_label="assets",
                model_name="manufacturer",
                user_id=user.pk,
                tenant_id=self.tenant.pk,
            )

        job.refresh_from_db()
        assert job.status == Job.STATUS_FAILED
        assert job.logs.endswith("The import could not be completed due to an unexpected error.")
        notification = Notification.objects.get(user=user, level=Notification.LEVEL_DANGER)
        assert "A system error occurred during the import" in notification.message
        record = next(record for record in caplog.records if hasattr(record, "import_context"))
        assert record.import_context["operation"] == "task.run"
        assert record.import_context["tenant_id"] == self.tenant.pk
        assert record.import_context["actor_id"] == user.pk
        assert record.import_context["exception_type"] == "RuntimeError"
        assert SECRET not in caplog.text
        assert SECRET not in job.logs

    @patch("core.tasks.csv_import.Notification.objects.create", side_effect=RuntimeError(SECRET))
    @patch("core.tasks.csv_import.Job.objects.get")
    def test_cleanup_failure_is_logged_safely_and_reraised(self, get_job, _create_notification, caplog):
        job = MagicMock()
        job.mark_running.return_value = True
        get_job.return_value = job

        with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match=SECRET):
            import_csv_task(99, [], "missing", "model", user_id=None, tenant_id=None)

        cleanup = next(record for record in caplog.records if record.import_context["operation"] == "task.cleanup")
        assert cleanup.import_context["exception_type"] == "RuntimeError"
        assert SECRET not in caplog.text


class TestMalformedImportPreview(SimpleTestCase):
    def test_malformed_rows_return_indexed_form_errors_without_enqueuing_jobs(self):
        malformed_inputs = [
            ("csv", "name,slug\nMaker,maker,extra", "Row 2: CSV row has more cells than headers."),
            (
                "yaml",
                "- name: Maker\n  slug: maker\n- invalid_scalar",
                "Row 2: YAML data elements must be mappings (key-value pairs).",
            ),
            (
                "yaml",
                "- name: Maker\n  slug: maker\n- null",
                "Row 2: YAML data elements must be mappings (key-value pairs).",
            ),
            (
                "yaml",
                "- name: Maker\n  slug: maker\n- [invalid, item]",
                "Row 2: YAML data elements must be mappings (key-value pairs).",
            ),
        ]

        with (
            patch("itambox.views.generic.import_.async_task") as enqueue,
            patch.object(Job.objects, "create") as create_job,
        ):
            for import_format, import_text, error in malformed_inputs:
                with self.subTest(import_format=import_format, import_text=import_text):
                    payload = {
                        "active_tab": "editor",
                        "import_format": import_format,
                        "import_text": import_text,
                        "delimiter": ",",
                        "_preview": "1",
                    }
                    request = RequestFactory().post("/import/", payload)
                    request.session = {}
                    request.user = AnonymousUser()
                    view = ObjectImportView()
                    view.model_form = _ConditionalImportForm
                    view.setup(request)

                    response = view.post(request)
                    response.render()
                    content = response.content.decode()
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(error, content)
                    self.assertNotIn("object has no attribute", content)
                    self.assertNotIn("import_rows", request.session)

        create_job.assert_not_called()
        enqueue.assert_not_called()
