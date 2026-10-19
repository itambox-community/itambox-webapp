"""Ordering of the blank choice in hand-assembled choice lists (issue #917).

Django puts ``ModelChoiceField.empty_label`` in front of the queryset choices
itself. Fields whose choices a form assembles by hand have no such guarantee,
so ``core.forms.choices`` normalises those lists and the filter forms apply it.
"""

from django import forms
from django.forms.models import ModelChoiceIterator
from django.test import SimpleTestCase

from core.forms.choices import apply_empty_choice_first, empty_choice_first


class EmptyChoiceFirstTest(SimpleTestCase):
    def test_blank_choice_moves_to_the_front(self):
        choices = [("a", "Alpha"), ("b", "Beta"), ("", "---------")]

        self.assertEqual(
            empty_choice_first(choices),
            [("", "---------"), ("a", "Alpha"), ("b", "Beta")],
        )

    def test_blank_choice_already_first_is_unchanged(self):
        choices = [("", "---------"), ("a", "Alpha")]

        self.assertEqual(empty_choice_first(choices), choices)

    def test_list_without_blank_choice_is_unchanged(self):
        choices = [("a", "Alpha"), ("b", "Beta")]

        self.assertEqual(empty_choice_first(choices), choices)

    def test_duplicate_blank_choices_collapse_to_one(self):
        choices = [("a", "Alpha"), ("", "---------"), ("", "No selection")]

        self.assertEqual(empty_choice_first(choices), [("", "---------"), ("a", "Alpha")])

    def test_ordering_is_idempotent(self):
        choices = [("a", "Alpha"), ("", "---------"), ("b", "Beta")]

        once = empty_choice_first(choices)

        self.assertEqual(empty_choice_first(once), once)

    def test_field_with_hand_assembled_choices_gets_the_blank_first(self):
        field = forms.ChoiceField(
            required=False,
            choices=[("a", "Alpha"), ("b", "Beta"), ("", "---------")],
        )

        apply_empty_choice_first(field)

        self.assertEqual([value for value, _label in field.choices], ["", "a", "b"])
        self.assertEqual([value for value, _label in field.widget.choices], ["", "a", "b"])

    def test_model_choice_field_keeps_djangos_empty_label_handling(self):
        class QuerysetStub:
            """Stands in for a queryset: no database, no rows."""

            def all(self):
                return self

        field = forms.ModelChoiceField(queryset=QuerysetStub(), required=False)

        apply_empty_choice_first(field)

        # Django's own iterator stays in charge of the blank entry; the helper
        # must not freeze it into a list.
        self.assertIsInstance(field.choices, ModelChoiceIterator)
        self.assertEqual(field.empty_label, "---------")

    def test_other_fields_are_returned_untouched(self):
        field = forms.CharField(required=False, initial="keep me")

        self.assertIs(apply_empty_choice_first(field), field)
        self.assertEqual(field.initial, "keep me")
