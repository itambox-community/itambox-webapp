"""Global search form: object-type choices are resolved from the live registry (#919).

``SEARCH_INDEXES`` is filled by ``@register_search`` while the app registry
becomes ready, i.e. after ``core.forms`` is imported. The choices must therefore
be built per form construction; a module-level constant stayed empty for the
whole process and left the "Object types" dropdown on ``/search/`` without any
options.
"""

import re

import pytest
from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from assets.models import Asset, AssetType, Manufacturer, StatusLabel
from core.forms import SearchForm
from core.search import SEARCH_INDEXES
from core.tests.mixins import TenantTestMixin
from extras.models import Tag

TOKEN = "ZQX919RIDGETOKEN"


def _choice_map(form):
    return dict(form.fields["obj_type"].choices)


@pytest.mark.django_db
class ObjectTypeChoicesTests(TestCase):
    """The choices come from the registry, evaluated per construction."""

    def test_choices_list_every_registered_index(self):
        choices = _choice_map(SearchForm())

        self.assertTrue(choices, "the object-type field must not be empty after registry startup")
        self.assertEqual(
            set(choices), {f"{model._meta.app_label}.{model._meta.model_name}" for model in SEARCH_INDEXES}
        )
        self.assertEqual(len(choices), len(SEARCH_INDEXES))

    def test_known_entry_keeps_the_app_pipe_model_label(self):
        choices = _choice_map(SearchForm())

        expected_label = f"{Asset._meta.app_label.capitalize()} | {str(Asset._meta.verbose_name).capitalize()}"
        self.assertEqual(choices["assets.asset"], expected_label)
        self.assertIn(" | ", choices["assets.asset"])

    def test_choices_are_evaluated_per_construction_not_frozen(self):
        extra_model = next(model for model in apps.get_models() if model not in SEARCH_INDEXES)
        key = f"{extra_model._meta.app_label}.{extra_model._meta.model_name}"

        self.assertNotIn(key, _choice_map(SearchForm()))

        SEARCH_INDEXES[extra_model] = [object()]
        try:
            choices = _choice_map(SearchForm())
        finally:
            del SEARCH_INDEXES[extra_model]

        self.assertIn(key, choices)
        self.assertNotIn(key, _choice_map(SearchForm()))

    def test_submitted_object_type_validates_against_the_registry(self):
        form = SearchForm({"q": "needle", "obj_type": ["assets.asset"]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["obj_type"], ["assets.asset"])


@pytest.mark.django_db
class SearchFormRenderTests(TenantTestMixin, TestCase):
    """The rendered select carries one option per registered index."""

    def setUp(self):
        self.setup_tenant_context(name="Search Form", slug="search-form")
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

    def test_search_page_renders_one_option_per_registered_index(self):
        response = self.client.get(reverse("search"))
        self.assertEqual(response.status_code, 200)

        html = response.content.decode()
        select = re.search(r'<select[^>]*id="id_obj_type_select"[^>]*>.*?</select>', html, re.DOTALL)
        self.assertIsNotNone(select, "the object-type select is not rendered")
        select_html = select.group(0)

        self.assertIn("data-tom-select", select_html)
        self.assertEqual(len(re.findall(r"<option\b", select_html)), len(SEARCH_INDEXES))
        self.assertIn('value="assets.asset"', select_html)


@pytest.mark.django_db
class SearchObjectTypeFilterTests(TenantTestMixin, TestCase):
    """Selecting an object type narrows the result groups to that model."""

    def setUp(self):
        self.setup_tenant_context(name="Search Filter", slug="search-filter")
        self.client_login_to_tenant(self.tenant_admin, self.tenant)

        manufacturer = Manufacturer.objects.create(name="ZQX Manufacturer", slug="zqx-manufacturer")
        asset_type = AssetType.objects.create(manufacturer=manufacturer, model="Ridge")
        status = StatusLabel.objects.create(name="Ridge Active", slug="ridge-active", type=StatusLabel.TYPE_DEPLOYABLE)
        Asset.objects.create(
            name=f"Asset {TOKEN}",
            asset_tag="ZQX9190001",
            asset_type=asset_type,
            status=status,
            tenant=self.tenant,
        )
        Tag.objects.create(name=TOKEN, slug="zqx919-ridgetoken")

    def _result_models(self, **params):
        response = self.client.get(reverse("search"), {"q": TOKEN, **params})
        self.assertEqual(response.status_code, 200)
        return set(response.context["results"])

    def test_without_object_type_both_types_are_searched(self):
        self.assertEqual(self._result_models(), {Asset, Tag})

    def test_single_object_type_restricts_the_result_groups(self):
        self.assertEqual(self._result_models(obj_type="assets.asset"), {Asset})
        self.assertEqual(self._result_models(obj_type="extras.tag"), {Tag})
