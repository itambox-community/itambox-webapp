"""Display-layer resolution of vocabulary labels through translation rows (#832).

This is the single resolution path for every surface that shows a definition,
choice, section (fieldset) or unit label. The chain is fixed: the requested
locale (or, without one, the active locale), then its base language, then the
base label stored on the entity. A missing or blank row is never an error and
never yields an empty string.

The base label is the canonical source-language text, so a translator for the
source language (``settings.LANGUAGE_CODE``) is inert and issues no queries.
Published payloads (REST, GraphQL) keep reading the base label directly and
must not use this module.
"""

from django.conf import settings
from django.utils import translation

from extras.models import CustomFieldChoiceTranslation, CustomFieldsetTranslation, CustomFieldTranslation

# Display token for a word-like canonical unit when no translation row
# overrides it; symbolic units (``Gbit/s``, ``VA``) are shown as stored.
BASE_UNIT_TOKENS = {"pages_per_minute": "ppm"}


def language_candidates(locale) -> tuple[str, ...]:
    """Normalised lookup order for one locale: the exact code, then its base language."""
    code = str(locale or "").strip().lower().replace("_", "-")
    if not code:
        return ()
    return tuple(dict.fromkeys((code, code.split("-", 1)[0])))


def _source_language() -> str:
    candidates = language_candidates(settings.LANGUAGE_CODE)
    return candidates[-1] if candidates else ""


def base_unit_text(definition) -> str:
    unit = getattr(definition, "canonical_unit", None) or ""
    return BASE_UNIT_TOKENS.get(unit, unit)


def _pk(entity):
    return getattr(entity, "pk", None)


class VocabularyTranslator:
    """Resolve vocabulary labels for one locale after a bulk :meth:`load`.

    Lookups never query: an entity that was not loaded resolves to its base
    label. ``load`` issues at most one query per entity kind and only for ids
    not loaded before, so a view can prime it once for everything it renders.
    """

    def __init__(self, requested_locale=None, active_locale=None):
        locale = requested_locale or active_locale or translation.get_language()
        self.candidates = language_candidates(locale)
        self.inert = not self.candidates or self.candidates[-1] == _source_language()
        self._field_rows: dict[int, dict] = {}
        self._choice_rows: dict[int, dict] = {}
        self._fieldset_rows: dict[int, dict] = {}
        self._loaded_fields: set[int] = set()
        self._loaded_choice_sets: set[int] = set()
        self._loaded_fieldsets: set[int] = set()

    # -- loading ---------------------------------------------------------

    def _rank(self, locale):
        return self.candidates.index(locale) if locale in self.candidates else len(self.candidates)

    def _store(self, target, rows, owner_key):
        best: dict[int, tuple[int, dict]] = {}
        for row in rows:
            rank = self._rank(row["locale"])
            owner = row[owner_key]
            if owner not in best or rank < best[owner][0]:
                best[owner] = (rank, row)
        for owner, (_rank, row) in best.items():
            target[owner] = row

    def load(self, *, fields=(), fieldsets=()):
        """Bulk-load rows for field definitions (with their choices) and fieldsets."""
        if self.inert:
            return self
        field_ids = {pk for pk in map(_pk, fields) if pk is not None} - self._loaded_fields
        choice_set_ids = {
            getattr(definition, "choice_set_id", None) for definition in fields
        } - {None} - self._loaded_choice_sets
        fieldset_ids = {pk for pk in map(_pk, fieldsets) if pk is not None} - self._loaded_fieldsets
        if field_ids:
            self._store(
                self._field_rows,
                CustomFieldTranslation.objects.filter(
                    custom_field_id__in=field_ids, locale__in=self.candidates
                ).values("custom_field_id", "locale", "label", "help_text", "unit_label"),
                "custom_field_id",
            )
            self._loaded_fields |= field_ids
        if choice_set_ids:
            self._store(
                self._choice_rows,
                CustomFieldChoiceTranslation.objects.filter(
                    choice__choice_set_id__in=choice_set_ids, locale__in=self.candidates
                ).values("choice_id", "locale", "label"),
                "choice_id",
            )
            self._loaded_choice_sets |= choice_set_ids
        if fieldset_ids:
            self._store(
                self._fieldset_rows,
                CustomFieldsetTranslation.objects.filter(
                    fieldset_id__in=fieldset_ids, locale__in=self.candidates
                ).values("fieldset_id", "locale", "label", "description"),
                "fieldset_id",
            )
            self._loaded_fieldsets |= fieldset_ids
        return self

    def load_resolved(self, resolved_fields, fieldsets=()):
        """Load the definitions of resolved fields plus the given fieldsets."""
        return self.load(fields=[resolved.definition for resolved in resolved_fields], fieldsets=fieldsets)

    # -- resolution ------------------------------------------------------

    @staticmethod
    def _pick(row, attribute, fallback):
        value = (row or {}).get(attribute) or ""
        return value if value.strip() else fallback

    def field_label(self, definition) -> str:
        base = getattr(definition, "label", "") or getattr(definition, "name", "")
        return self._pick(self._field_rows.get(_pk(definition)), "label", base)

    def field_help_text(self, definition) -> str:
        base = getattr(definition, "help_text", "") or ""
        return self._pick(self._field_rows.get(_pk(definition)), "help_text", base)

    def unit(self, definition) -> str:
        return self._pick(self._field_rows.get(_pk(definition)), "unit_label", base_unit_text(definition))

    def choice_label(self, choice) -> str:
        base = str(getattr(choice, "label", "") or getattr(choice, "key", ""))
        return self._pick(self._choice_rows.get(_pk(choice)), "label", base)

    def fieldset_label(self, fieldset) -> str:
        base = getattr(fieldset, "label", "") or getattr(fieldset, "slug", "")
        return self._pick(self._fieldset_rows.get(_pk(fieldset)), "label", base)

    def fieldset_description(self, fieldset) -> str:
        base = getattr(fieldset, "description", "") or ""
        return self._pick(self._fieldset_rows.get(_pk(fieldset)), "description", base)


def vocabulary_translator(*, fields=(), fieldsets=(), requested_locale=None, active_locale=None):
    """Build a translator for the requested (or active) locale and load the given entities."""
    return VocabularyTranslator(requested_locale, active_locale).load(fields=list(fields), fieldsets=list(fieldsets))


BASE_LABELS = VocabularyTranslator(requested_locale=settings.LANGUAGE_CODE)
"""Inert translator: base labels only. Default for callers that pass none."""
