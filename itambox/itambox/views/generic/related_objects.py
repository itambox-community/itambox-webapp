"""The detail view's "Related Objects" sidebar.

Extracted from ``ObjectDetailView`` so the batching strategy below lives in one
place and can be tested without standing up a request. ``ObjectDetailView``
keeps a thin ``_build_related_objects_list`` wrapper for subclasses that
override or call it.
"""

from django.core.exceptions import FieldDoesNotExist
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse

from core.managers import Scope
from itambox.utils import get_model_viewname


def _scoped_default(model):
    """The model's default manager queryset, scoped explicitly to the current scope."""
    queryset = model._default_manager.get_queryset()
    if hasattr(queryset, "for_scope"):
        queryset = queryset.for_scope(Scope.current())
    return queryset


class RelatedObjectProvider:
    """Build the label/count/url rows for an object's reverse relations.

    H4 batching: the legacy implementation issued one ``.count()`` query per
    auto-created reverse relation (~10-15 separate COUNTs per detail GET). Each
    of those counts went through the related model's *default* manager
    (``_default_manager.get_queryset()``) — i.e. tenant scoping + soft-delete
    filtering — because Django's reverse related manager subclasses the related
    model's default manager and calls ``super().get_queryset()`` before applying
    the FK filter.

    We reproduce the *identical* counts with far fewer queries by annotating the
    single object's row with one correlated ``Subquery`` COUNT per reverse FK /
    one-to-one relation. Each subquery is built from the related model's
    ``_default_manager`` and filtered ``<fk>=OuterRef(<target>)``, so it carries
    exactly the same WHERE clauses (tenant + soft-delete) the old ``.count()``
    applied. Independent per-relation subqueries (not one multi-join aggregate)
    avoid the cartesian fan-out that would inflate counts. The outer query uses
    ``_base_manager`` purely to fetch the single pk row — the subqueries do their
    own default-manager scoping, so the outer manager's filtering does not affect
    any displayed count.

    Reverse many-to-many relations are NOT batched: their count needs a
    through-table join, which a plain FK subquery cannot reproduce, so we keep
    the per-relation ``.count()`` for those (a handful at most). Any relation
    whose subquery can't be built safely also falls back to ``.count()``.
    """

    def __init__(self, obj, *, excluded_model_labels=()):
        self.obj = obj
        self.excluded_model_labels = frozenset(excluded_model_labels)

    @staticmethod
    def count_uses_distinct(related_model):
        """Return True when the related model's default-manager queryset applies
        ``.distinct()`` — i.e. it has a ``filter_tenants`` M2M, so tenant scoping
        joins that table and de-duplicates rows (see
        ``TenantScopingQuerySet.filter_by_tenant``). For such models a
        ``.values().annotate(Count('pk'))`` subquery would count the M2M-join
        fan-out instead of distinct rows, miscounting. We keep the legacy
        ``.count()`` (which counts distinct rows) for these relations.
        """
        try:
            related_model._meta.get_field("filter_tenants")
            return True
        except FieldDoesNotExist:
            return False

    def _collect_relations(self):
        """First pass: metadata for every relation the legacy loop considered,
        plus a staged ``Subquery`` annotation for each batchable relation.

        Returns ``(meta, annotations)`` where ``meta`` preserves iteration order
        so the assembled list matches the legacy pre-sort order exactly (the
        final sort by label makes order deterministic regardless).
        """
        obj = self.obj
        meta = []  # list of (relation, related_model, accessor_name, count_key|None)
        annotations = {}
        for relation in obj._meta.get_fields(include_parents=True):
            if not relation.is_relation or relation.concrete:
                continue
            if relation.auto_created and not relation.concrete:
                related_model = relation.related_model
                if not related_model:
                    continue
                if related_model._meta.label_lower in self.excluded_model_labels:
                    continue

                accessor_name = relation.get_accessor_name()
                if not accessor_name or not hasattr(obj, accessor_name):
                    continue

                count_key = None
                if not relation.many_to_many and not self.count_uses_distinct(related_model):
                    # Reverse FK / one-to-one: batch via a correlated subquery
                    # through the related model's DEFAULT manager so the exact
                    # tenant + soft-delete filtering of the old .count() is kept.
                    try:
                        fk_name = relation.field.name
                        target = getattr(relation, "field_name", None) or "pk"
                        subquery = Subquery(
                            _scoped_default(related_model)
                            .filter(**{fk_name: OuterRef(target)})
                            .order_by()
                            .values(fk_name)
                            .annotate(c=Count("pk"))
                            .values("c")[:1]
                        )
                        count_key = f"_relcount_{len(annotations)}"
                        annotations[count_key] = Coalesce(subquery, 0)
                    # broad except: render-degrade: a relation the batch subquery cannot express keeps its own count
                    except Exception:
                        # Couldn't stage the subquery — fall back to .count().
                        count_key = None

                meta.append((relation, related_model, accessor_name, count_key))
        return meta, annotations

    def _annotate(self, annotations):
        """Single query: annotate the one object row with every staged subquery
        COUNT. ``_base_manager`` guarantees the pk row is returned irrespective
        of the model's own scoping; the subqueries scope themselves independently.
        """
        if not annotations:
            return None
        try:
            # unscoped: re-reads the already-authorized detail object by pk to attach counts
            return type(self.obj)._base_manager.filter(pk=self.obj.pk).annotate(**annotations).first()
        # broad except: render-degrade: a failed batch count falls back to per-relation counts
        except Exception:
            return None

    def build(self):
        """Return the sorted ``[{"label", "count", "url"}, ...]`` sidebar rows."""
        obj = self.obj
        meta, annotations = self._collect_relations()
        annotated = self._annotate(annotations)

        # Second pass: resolve each relation's count (from the annotated row when
        # available, else a direct .count()) and assemble the list identically.
        related_objects_list = []
        for relation, related_model, accessor_name, count_key in meta:
            count = None
            if count_key is not None and annotated is not None:
                count = getattr(annotated, count_key, None)
            if count is None:
                # M2M relations, un-batchable relations, or a failed batch query
                # keep the legacy per-relation count (identical to before).
                try:
                    count = getattr(obj, accessor_name).count()
                # broad except: render-degrade: one uncountable relation drops out of the sidebar, not the detail page
                except Exception:
                    continue

            if count > 0:
                # Resolve the list viewname via get_model_viewname so core-app
                # reverse-relation targets (root-mounted, UN-namespaced) resolve
                # too — a hardcoded '{app}:{model}_list' silently dropped them.
                # App-namespaced targets still map to '{app}:{model}_list'.
                view_name = get_model_viewname(related_model, "list")

                try:
                    base_url = reverse(view_name)
                    filter_field_name = relation.remote_field.name if relation.remote_field else obj._meta.model_name
                    filter_val = getattr(obj, "slug", obj.pk)
                    url = f"{base_url}?{filter_field_name}={filter_val}"
                    label = str(related_model._meta.verbose_name_plural).title()

                    related_objects_list.append(
                        {
                            "label": label,
                            "count": count,
                            "url": url,
                        }
                    )
                except NoReverseMatch:
                    continue

        related_objects_list.sort(key=lambda x: x["label"])
        return related_objects_list
