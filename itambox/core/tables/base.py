import logging

import django_tables2 as tables
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.utils.translation import gettext_lazy as _
from django_tables2.data import TableQuerysetData

from core.paginator import EnhancedPaginator
from core.tables.columns import IDColumn
from core.tables.constants import TABLE_EMPTY_VALUE
from itambox.utils import get_paginate_count

logger = logging.getLogger(__name__)


class BaseTable(tables.Table):
    exempt_columns = ("pk", "actions")
    # Presentation opt-out; server-side bulk authorization remains authoritative.
    bulk_edit_enabled = True

    # Universal detail-link column, hidden by default. Sits immediately after
    # the checkbox as the first data column when shown. Tables with no natural
    # identity column (e.g. Warranty/Reservation/Disposal) re-declare it with
    # ``visible=True``; every other table exposes it via the column selector.
    id = IDColumn(visible=False)

    class Meta:
        model = None
        template_name = "global_includes/htmx_table.html"
        attrs = {
            "class": "table table-hover table-vcenter card-table",
            "thead": {"class": "text-nowrap"},
            "td": {"class": "text-nowrap"},
        }
        exclude_from_config = ("pk", "actions")
        default = TABLE_EMPTY_VALUE

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", TABLE_EMPTY_VALUE)
        super().__init__(*args, **kwargs)

        if self.empty_text is None:
            model = getattr(self.Meta, "model", None)
            if model:
                self.empty_text = _("No {model_name} found").format(model_name=model._meta.verbose_name_plural)
            else:
                self.empty_text = _("No results found")

        self._apply_default_sequence()
        self._apply_column_width_classes()

    def _apply_default_sequence(self):
        """Establish a sane default column order at construction time.

        List views run the custom ``configure()`` which calls ``_set_columns()``
        and builds an explicit ``sequence``. Detail-view tabs, however, only run
        django_tables2's ``RequestConfig().configure()``, which never touches the
        sequence — so columns fall back to "declared + Meta.fields" order, dropping
        ``pk`` out of front and ``checkout_checkin``/``actions`` into the middle.
        Anchoring the sequence here keeps both paths consistent: ``pk`` first,
        ``actions`` last, otherwise honouring ``default_columns``/``fields`` order.
        """
        meta = getattr(self, "Meta", None)
        preferred = list(getattr(meta, "default_columns", None) or getattr(meta, "fields", None) or [])
        if not preferred:
            return

        all_names = list(self.columns.names())
        sequence = [c for c in preferred if c in all_names]
        sequence += [c for c in all_names if c not in sequence]

        if "pk" in sequence:
            sequence.remove("pk")
            sequence.insert(0, "pk")
        if "id" in sequence:
            sequence.remove("id")
            sequence.insert(1 if (sequence and sequence[0] == "pk") else 0, "id")
        for trailing in ("checkout_checkin", "actions"):
            if trailing in sequence:
                sequence.remove(trailing)
                sequence.append(trailing)

        self.sequence = sequence

    def _apply_column_width_classes(self):
        width_heuristics = {
            "name": "col-name",
            "description": "col-name",
            "serial_number": "col-serial",
            "asset_tag": "col-tag",
            "part_number": "col-tag",
            "tenant": "col-relation",
            "location": "col-relation",
            "supplier": "col-relation",
            "manufacturer": "col-relation",
            "assignee": "col-relation",
            "contacts": "col-relation",
            "actions": "col-actions",
        }
        for col_name, width_class in width_heuristics.items():
            if col_name not in self.columns:
                continue
            base_column = self.columns[col_name].column
            if base_column.attrs is None:
                base_column.attrs = {}
            for attr_key in ("th", "td"):
                cell_attrs = base_column.attrs.setdefault(attr_key, {})
                current_class = cell_attrs.get("class", "")
                if "col-actions-wide" in current_class and width_class == "col-actions":
                    continue
                cell_attrs["class"] = f"{current_class} {width_class}".strip()

    def _get_columns(self, visible=True):
        columns = []
        for name, column in self.columns.items():
            if column.visible == visible and name not in self.exempt_columns:
                columns.append((name, column.verbose_name))
        return columns

    @property
    def name(self):
        return self.__class__.__name__

    @property
    def available_columns(self):
        return sorted(self._get_columns(visible=False))

    @property
    def selected_columns(self):
        return self._get_columns(visible=True)

    def _set_columns(self, selected_columns):
        # iterall() (not the default iterator, which yields only visible
        # columns) so we can re-show a column that is hidden by default
        # (e.g. the `id` detail-link column) when it is explicitly selected.
        for column in self.columns.iterall():
            if column.name in selected_columns and not column.visible:
                self.columns.show(column.name)
            elif column.name not in [*selected_columns, *self.exempt_columns]:
                self.columns.hide(column.name)
            elif column.name in self.exempt_columns and not column.visible:
                self.columns.show(column.name)

        base_column_names = set(self.columns.names())
        self.sequence = [
            *[c for c in selected_columns if c in base_column_names],
            *[c for c in base_column_names if c not in selected_columns],
        ]

        if "pk" in self.sequence:
            self.sequence.remove("pk")
            self.sequence.insert(0, "pk")

        if "id" in self.sequence:
            self.sequence.remove("id")
            self.sequence.insert(1 if (self.sequence and self.sequence[0] == "pk") else 0, "id")

        if "actions" in self.sequence:
            self.sequence.remove("actions")
            self.sequence.append("actions")

    def _apply_prefetching(self, columns=None):
        if not isinstance(self.data, TableQuerysetData):
            return

        select_related_fields = []
        prefetch_related_fields = []

        for column in self.columns.iterall():
            if columns is not None:
                if column.name not in columns:
                    continue
            elif not column.visible:
                continue

            # Add any explicit prefetches declared by the column class itself
            if hasattr(column.column, "get_prefetch_fields"):
                prefetch_related_fields.extend(column.column.get_prefetch_fields())

            model = getattr(self.Meta, "model", None)
            if model is None:
                continue
            accessor = column.accessor
            if accessor.startswith("custom_field_data__"):
                continue
            prefetch_path = []
            use_select_related = True

            # Normalize and split relation path using both dot (.) and double-underscore (__) separators
            path_parts = str(accessor).replace("__", ".").split(".")
            for field_name in path_parts:
                try:
                    field = model._meta.get_field(field_name)
                except FieldDoesNotExist:
                    break
                if hasattr(field, "remote_field") and field.remote_field:
                    prefetch_path.append(field_name)
                    # If this step in the path is a many-to-many or reverse foreign key relation,
                    # we must fetch this entire path branch using prefetch_related.
                    if getattr(field, "many_to_many", False) or getattr(field, "one_to_many", False):
                        use_select_related = False
                    model = field.remote_field.model
                else:
                    break

            if prefetch_path:
                full_path = "__".join(prefetch_path)
                if use_select_related:
                    select_related_fields.append(full_path)
                else:
                    prefetch_related_fields.append(full_path)

        if select_related_fields:
            # Special optimization for AssetType: since AssetType.__str__ accesses its related manufacturer,
            # we must always select_related manufacturer when select_related-ing asset_type.
            extended_select = []
            for path in select_related_fields:
                extended_select.append(path)
                if path == "asset_type":
                    extended_select.append("asset_type__manufacturer")
                elif path.endswith("__asset_type"):
                    extended_select.append(path + "__manufacturer")
            unique_select = list(set(extended_select))
            self.data.data = self.data.data.select_related(*unique_select)
        if prefetch_related_fields:
            # Validate prefetch paths against the Meta model to prevent AttributeError on models without relations
            model_class = getattr(self.Meta, "model", None)
            valid_prefetch = []
            for path in prefetch_related_fields:
                if model_class is None:
                    continue
                parts = path.split("__")
                current_model = model_class
                valid = True
                for part in parts:
                    try:
                        field = current_model._meta.get_field(part)
                        if hasattr(field, "remote_field") and field.remote_field:
                            current_model = field.remote_field.model
                    except FieldDoesNotExist:
                        valid = False
                        break
                if valid:
                    valid_prefetch.append(path)

            if valid_prefetch:
                unique_prefetch = list(set(valid_prefetch))
                self.data.data = self.data.data.prefetch_related(*unique_prefetch)

    def has_perm(self, user, perm, record=None):
        if not user:
            return False
        if record is None:
            return user.has_perm(perm)

        # Extract tenant id to cache per tenant
        tenant_id = getattr(record, "tenant_id", None)
        if tenant_id is None:
            tenant = getattr(record, "tenant", None)
            if tenant:
                tenant_id = getattr(tenant, "pk", None)
            elif record.__class__.__name__.lower() == "tenant":
                tenant_id = record.pk

        # Use tenant ID or record ID if no tenant exists
        if tenant_id is not None:
            cache_key = f"_perm_cache_t{tenant_id}_{perm}"
        else:
            cache_key = f"_perm_cache_r{record.pk}_{perm}"

        if not hasattr(self, cache_key):
            setattr(self, cache_key, user.has_perm(perm, record))
        return getattr(self, cache_key)

    def configure(self, request, paginate=True):
        columns = None
        ordering = None

        if request.user.is_authenticated and self.prefixed_order_by_field in request.GET:
            if request.GET[self.prefixed_order_by_field]:
                ordering = request.GET.getlist(self.prefixed_order_by_field)
            else:
                ordering = None

        if request.user.is_authenticated:
            try:
                UserPreference = apps.get_model("users", "UserPreference")
                if not hasattr(request, "_user_preferences_cache"):
                    request._user_preferences_cache = UserPreference.objects.filter(user=request.user).first()
                prefs = request._user_preferences_cache
                if prefs and prefs.data:
                    model = getattr(self.Meta, "model", None)
                    if model:
                        app_label = model._meta.app_label
                        user_config = prefs.data.get("tables", {}).get(app_label, {}).get(self.name, {})
                        if columns is None:
                            cols_from_pref = user_config.get("columns")
                            # Treat empty list same as absent key — fall through
                            # to Meta.default_columns / Meta.fields below
                            if cols_from_pref:
                                columns = cols_from_pref
            except (AttributeError, TypeError):
                # A stored preference of an unexpected shape falls back to the default columns.
                logger.debug("Error reading user column preferences for table %s", self.name)

        if columns is None:
            columns = getattr(self.Meta, "default_columns", self.Meta.fields)

        self._set_columns(columns)

        if columns_param := request.GET.get("include_columns"):
            for column_name in columns_param.split(","):
                if column_name in self.columns.names():
                    self.columns.show(column_name)
        if exclude_columns := request.GET.get("exclude_columns"):
            exclude_columns = exclude_columns.split(",")
            for column_name in exclude_columns:
                if column_name in self.columns.names() and column_name not in self.exempt_columns:
                    self.columns.hide(column_name)

        self._apply_prefetching()
        if ordering is not None:
            self.order_by = ordering

        if paginate:
            paginate_config = {"paginator_class": EnhancedPaginator, "per_page": get_paginate_count(request)}
        else:
            paginate_config = False
        tables.RequestConfig(request, paginate_config).configure(self)
