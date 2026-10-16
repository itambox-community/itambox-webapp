from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import resolve, reverse

from assets.forms.assettype_form import AssetTypeForm
from extras.models import CustomFieldset
from extras.services.vocabulary_translations import VocabularyTranslator


class SpecificationUiIntegrationTests(SimpleTestCase):
    def test_selected_cards_preserve_composition_order(self):
        def section(pk, slug):
            return SimpleNamespace(
                pk=pk,
                namespace="local",
                slug=slug,
                label=slug,
                description="",
                lifecycle=CustomFieldset.LIFECYCLE_ACTIVE,
                field_memberships=SimpleNamespace(all=lambda: []),
            )

        alpha, middle, zulu = section(1, "alpha"), section(2, "middle"), section(3, "zulu")
        form = object.__new__(AssetTypeForm)
        form._stored_custom_values = lambda: {}
        form._fieldset_source = lambda value: "local"
        # Source-language translator: inert, so the database-free test issues no queries.
        form.vocabulary_translator = VocabularyTranslator(active_locale="en")
        with patch("assets.forms.assettype_form.CustomFieldset.objects.filter") as query:
            query.return_value.prefetch_related.return_value.order_by.return_value = [alpha, middle, zulu]
            options = form._fieldset_options([zulu, alpha])
        self.assertEqual([option["id"] for option in options], [3, 1, 2])
        self.assertEqual([option["selected"] for option in options], [True, True, False])

    def test_choice_management_routes_are_reachable(self):
        routes = {
            "definition_choice_set_list": {},
            "definition_choice_set_add": {},
            "definition_choice_set_detail": {"pk": 1},
            "definition_choice_set_edit": {"pk": 1},
            "definition_choice_set_retire": {"pk": 1},
            "definition_choice_add": {"choice_set_pk": 1},
            "definition_choice_edit": {"choice_set_pk": 1, "pk": 2},
            "definition_choice_retire": {"choice_set_pk": 1, "pk": 2},
        }
        for name, kwargs in routes.items():
            with self.subTest(route=name):
                match = resolve(reverse(f"extras:{name}", kwargs=kwargs))
                self.assertEqual(match.view_name, f"extras:{name}")
                self.assertEqual(match.func.view_class.__module__, "extras.definition_views")
