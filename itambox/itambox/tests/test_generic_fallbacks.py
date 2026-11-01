"""Fallbacks of the generic views, tables and change-log rendering.

These paths used to sit behind broad ``except Exception`` handlers. They now catch
only what the call can raise (``NoReverseMatch``, ``FieldDoesNotExist``, a missing or
malformed related row) and keep the same visible fallback.
"""

import datetime

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase
from django.urls import reverse
from model_bakery import baker

from assets.models import Asset, AssetType
from assets.tables import AssetTable
from core.models import ObjectChange
from extras.models import Tag
from itambox.utils import model_view_url
from itambox.views.features import resolve_serialized_data
from itambox.views.generic import ObjectEditView
from procurement.models import Contract
from users.models import UserPreference

User = get_user_model()


class _PlainTagForm(forms.ModelForm):
    """A model form without a crispy helper, so the edit view builds one."""

    class Meta:
        model = Tag
        fields = ["name", "slug"]


def _href(url):
    return 'href="' + url + '"'


class _TagEditView(ObjectEditView):
    queryset = Tag.objects.all()
    model_form = _PlainTagForm


class ModelViewUrlTests(TestCase):
    def test_existing_view_resolves_and_missing_view_is_none(self):
        self.assertEqual(model_view_url(Tag, "list"), reverse("extras:tag_list"))
        self.assertIsNone(model_view_url(Tag, "no_such_action"))


class EditViewCancelUrlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cancel-url", password="x", is_superuser=True)

    def _cancel_href(self, obj):
        request = RequestFactory().get("/")
        request.user = self.user
        view = _TagEditView()
        view.setup(request)
        view.object = obj
        form = view.get_form()
        cancel = [field for field in form.helper.layout.fields if "Cancel" in str(getattr(field, "html", ""))]
        self.assertEqual(len(cancel), 1)
        return cancel[0].html

    def test_new_object_cancels_to_the_list(self):
        self.assertIn(_href(reverse("extras:tag_list")), self._cancel_href(None))

    def test_unsaved_object_without_a_detail_url_cancels_to_the_list(self):
        self.assertIn(_href(reverse("extras:tag_list")), self._cancel_href(Tag(name="Unsaved")))

    def test_saved_object_cancels_to_its_detail_page(self):
        tag = Tag.objects.create(name="Saved", slug="saved")
        self.assertIn(_href(tag.get_absolute_url()), self._cancel_href(tag))


class EditViewNextUrlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="next-url", email="next@example.com", password="x")
        self.client.force_login(self.user)
        self.contract = Contract.objects.create(
            name="Next URL Contract",
            contract_number="CTR-NEXT-001",
            contract_type="support",
            status="draft",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
        )
        self.url = reverse("procurement:contract_edit", kwargs={"pk": self.contract.pk})

    def _payload(self, **extra):
        payload = {
            "name": "Next URL Contract",
            "contract_number": "CTR-NEXT-001",
            "contract_type": "support",
            "status": "draft",
            "currency": "EUR",
            "billing_cycle": "annual",
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",
        }
        payload.update(extra)
        return payload

    def test_continue_returns_to_the_edit_form(self):
        response = self.client.post(self.url, self._payload(_continue="1"))
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

    def test_add_another_without_an_add_view_falls_back_to_the_success_url(self):
        # Contracts have a "create" view, not an "add" view.
        self.assertIsNone(model_view_url(Contract, "add"))
        response = self.client.post(self.url, self._payload(_addanother="1"))
        self.assertRedirects(response, self.contract.get_absolute_url(), fetch_redirect_response=False)


class TablePreferenceFallbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="table-prefs", password="x", is_superuser=True)

    def _visible_columns(self):
        request = RequestFactory().get("/")
        request.user = self.user
        table = AssetTable(Asset.objects.none())
        table.configure(request)
        return [column.name for column in table.columns]

    def test_a_malformed_stored_preference_falls_back_to_the_default_columns(self):
        defaults = self._visible_columns()
        UserPreference.objects.create(user=self.user, data={"tables": "not-a-mapping"})
        self.assertEqual(self._visible_columns(), defaults)


class ResolveSerializedDataTests(TestCase):
    def test_relations_resolve_to_labels_and_unresolvable_rows_say_deleted(self):
        asset_type = baker.make(AssetType)
        tag = Tag.objects.create(name="Alpha", slug="alpha")

        resolved = resolve_serialized_data(
            Asset,
            {
                "name": "Laptop",
                "asset_type": asset_type.pk,
                "tags": [tag.pk, 999999],
                "status": "not-a-key",
                "location": None,
                "no_such_field": "kept",
            },
        )

        self.assertEqual(resolved["name"], "Laptop")
        self.assertEqual(resolved["asset_type"], str(asset_type))
        self.assertEqual(resolved["tags"], ["Alpha", "tag #999999 (deleted)"])
        self.assertEqual(resolved["status"], "statuslabel #not-a-key (deleted)")
        self.assertIsNone(resolved["location"])
        self.assertEqual(resolved["no_such_field"], "kept")

    def test_generic_foreign_keys_resolve_or_keep_their_raw_id(self):
        tag = Tag.objects.create(name="Beta", slug="beta")
        tag_type = ContentType.objects.get_for_model(Tag)
        stale_type = ContentType.objects.create(app_label="removed_app", model="gone")

        def resolve(content_type_id, object_id):
            data = {"changed_object_type": content_type_id, "changed_object_id": object_id}
            return resolve_serialized_data(ObjectChange, data)["changed_object_id"]

        self.assertEqual(resolve(tag_type.pk, tag.pk), "Beta")
        self.assertEqual(resolve(tag_type.pk, 999999), 999999)  # target row gone
        self.assertEqual(resolve(stale_type.pk, 5), 5)  # model no longer installed
        self.assertEqual(resolve(999999, 5), 5)  # content type gone
