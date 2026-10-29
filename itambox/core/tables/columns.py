import django_tables2 as tables
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from core.html_styles import color_chip_class, safe_hex_color
from core.tables.constants import TABLE_EMPTY_VALUE
from itambox.utils import get_model_viewname


class ColorChipColumn(tables.Column):
    """Renders a colour dot + name linking to the object's detail page. The
    accessor must resolve to an instance exposing ``.color``, ``.name`` and
    ``.get_absolute_url()`` (or None) — e.g. Category or AssetRole, both of which
    carry a user-pickable colour."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("empty_values", ())  # always call render, even for None
        super().__init__(*args, **kwargs)

    def render(self, value):
        if not value:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)
        safe_color = safe_hex_color(value.color)
        color_class, style_block = color_chip_class(safe_color)
        return format_html(
            '{}<a href="{}" class="text-reset text-decoration-none d-inline-flex align-items-center">'
            '<span class="color-chip-dot d-inline-block rounded-circle me-1 {}"></span>{}</a>',
            style_block,
            value.get_absolute_url(),
            color_class,
            value.name,
        )


class CountLinkColumn(tables.Column):
    """A numeric column that links its count to a pre-filtered list view.

    Renders ``value`` as a link to ``reverse(viewname)?<url_param>=<record.pk>``; an
    empty/zero value — or a row with no record, e.g. a table footer — renders as a plain
    ``0`` with no link. Replaces the hand-written ``render_<x>_count`` methods that built
    this identical link inline across the app's tables (so the link format and the
    empty/zero fallback live in one place).

    Usage::

        asset_count = CountLinkColumn('assets:asset_list', 'status', verbose_name=_('Assets'))
    """

    def __init__(self, viewname, url_param, *args, **kwargs):
        self.viewname = viewname
        self.url_param = url_param
        super().__init__(*args, **kwargs)

    def render(self, value, record=None):
        if record is not None and value:
            url = f"{reverse(self.viewname)}?{self.url_param}={record.pk}"
            return format_html('<a href="{}">{}</a>', url, value)
        return value or 0


class BooleanColumn(tables.Column):
    TRUE_MARK = mark_safe('<span class="text-success"><i class="mdi mdi-check-circle-outline"></i></span>')
    FALSE_MARK = mark_safe('<span class="text-danger"><i class="mdi mdi-close-circle-outline"></i></span>')

    def __init__(self, *args, true_mark=None, false_mark=None, **kwargs):
        self.true_mark = true_mark if true_mark is not None else self.TRUE_MARK
        self.false_mark = false_mark if false_mark is not None else self.FALSE_MARK
        super().__init__(*args, **kwargs)

    def render(self, value):
        if value is True:
            return self.true_mark
        if value is False:
            return self.false_mark
        return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)


class IDColumn(tables.Column):
    """Primary-key column that links to the record's own detail view.

    Inherited by every :class:`BaseTable` but hidden by default; it can be
    enabled per-table via the column selector, or declared explicitly with
    ``visible=True`` on tables that have no natural identity column (so a row
    is always reachable). NetBox-style convention: whatever the first visible
    column is, it links to the object's detail view — this guarantees one
    column always does, even when there is nothing meaningful to linkify.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("accessor", "pk")
        kwargs.setdefault("verbose_name", _("ID"))
        kwargs.setdefault("linkify", self._detail_url)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _detail_url(record):
        get_url = getattr(record, "get_absolute_url", None)
        if get_url is None:
            return None
        try:
            return get_url()
        # broad except: render-degrade: a record whose detail URL cannot be built shows its id without a link
        except Exception:
            return None


class ToggleColumn(tables.CheckBoxColumn):
    def __init__(self, *args, **kwargs):
        default = kwargs.pop("default", "")
        visible = kwargs.pop("visible", True)
        if "attrs" not in kwargs:
            kwargs["attrs"] = {
                "th": {
                    "class": "w-1 text-nowrap",
                    "aria-label": _("Select all"),
                },
                "td": {
                    "class": "w-1 text-nowrap",
                },
                "input": {
                    "class": "form-check-input",
                    "aria-label": _("Select row"),
                },
            }
        super().__init__(*args, default=default, visible=visible, **kwargs)

    @property
    def header(self):
        title_text = _("Toggle all")
        return format_html(
            '<input type="checkbox" class="toggle form-check-input" name="select_all" '
            'data-select-all="true" title="{}" aria-label="{}" />',
            title_text,
            title_text,
        )


class ActionsColumn(tables.Column):
    attrs = {
        "th": {
            "class": "col-actions text-nowrap",
        },
        "td": {"class": "text-end text-nowrap noprint p-1 col-actions"},
    }
    empty_values = ()
    verbose_name = ""
    orderable = False

    actions = {
        "edit": {"title": _("Edit"), "icon": "pencil", "css_class": "primary"},
        "delete": {"title": _("Delete"), "icon": "trash", "css_class": "danger"},
    }

    def __init__(self, *args, actions=("edit", "delete"), extra_buttons="", split_actions=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra_buttons = extra_buttons
        self.split_actions = split_actions
        self.actions = {name: self.actions[name] for name in actions if name in self.actions}

    def render(self, record, table, **kwargs):
        if not self.actions and not self.extra_buttons:
            return ""
        if not getattr(record, "pk", None):
            return ""

        model = type(record)

        is_deleted = getattr(record, "deleted_at", None) is not None
        if is_deleted:
            ct = ContentType.objects.get_for_model(record)

            restore_url = reverse("object_restore", kwargs={"content_type_id": ct.pk, "object_id": record.pk})
            purge_url = reverse("object_purge", kwargs={"content_type_id": ct.pk, "object_id": record.pk})

            restore_title = _("Restore")
            purge_title = _("Delete Permanently")

            restore_confirm = _("Are you sure you want to restore this {model_name}?").format(
                model_name=model._meta.verbose_name
            )
            purge_confirm = _(
                "Are you sure you want to PERMANENTLY delete this {model_name}? This action cannot be undone!"
            ).format(model_name=model._meta.verbose_name)

            restore_btn = (
                f'<a class="btn btn-sm btn-soft-success me-1" href="{restore_url}" '
                f'hx-post="{restore_url}" hx-target="#object-list-dynamic-content" '
                f'hx-confirm="{restore_confirm}" '
                f'title="{restore_title}" aria-label="{restore_title}">'
                f'<i class="mdi mdi-backup-restore"></i></a>'
            )
            purge_btn = (
                f'<a class="btn btn-sm btn-soft-danger" href="{purge_url}" '
                f'hx-post="{purge_url}" hx-target="#object-list-dynamic-content" '
                f'hx-confirm="{purge_confirm}" '
                f'title="{purge_title}" aria-label="{purge_title}">'
                f'<i class="mdi mdi-delete-forever"></i></a>'
            )

            return mark_safe(restore_btn + purge_btn)

        icon_edit = '<i class="mdi mdi-pencil-outline"></i>'
        icon_delete = '<i class="mdi mdi-trash-can-outline"></i>'

        icons = {
            "edit": icon_edit,
            "delete": icon_delete,
        }

        html = ""
        button = None
        dropdown_links = []

        for idx, (action, attrs) in enumerate(self.actions.items()):
            icon = icons.get(action, "")
            viewname = get_model_viewname(model, "update" if action == "edit" else "delete")
            url = None
            for kwargs in ({"pk": record.pk}, {"slug": getattr(record, "slug", None)}):
                if None in kwargs.values():
                    continue
                try:
                    url = reverse(viewname, kwargs=kwargs)
                    break
                except NoReverseMatch:
                    continue
            if url is None:
                continue

            if len(self.actions) == 1 or (self.split_actions and idx == 0):
                # Ghost treatment: monochrome at rest, accent on hover
                # (indigo for edit, red for delete). Keeps rows calm.
                ghost_class = "btn-action btn-action-danger" if action == "delete" else "btn-action"
                button = (
                    f'<a class="btn btn-sm {ghost_class}" href="{url}" type="button" '
                    f'title="{attrs["title"]}" aria-label="{attrs["title"]}">{icon}</a>'
                )
            else:
                dropdown_links.append(f'<li><a class="dropdown-item" href="{url}">{icon} {attrs["title"]}</a></li>')

        # Changelog — link to the object's detail page changelog tab, shown at
        # the top of the dropdown for every model with a detail view. The
        # destructive Delete entry stays last, behind a divider.
        detail_viewname = get_model_viewname(model, "detail")
        for detail_kwargs in ({"pk": record.pk}, {"slug": getattr(record, "slug", None)}):
            if None in detail_kwargs.values():
                continue
            try:
                detail_url = reverse(detail_viewname, kwargs=detail_kwargs)
            except NoReverseMatch:
                continue
            changelog_title = _("Changelog")
            changelog_li = (
                f'<li><a class="dropdown-item" href="{detail_url}?tab=changelog">'
                f'<i class="mdi mdi-history"></i> {changelog_title}</a></li>'
            )
            if dropdown_links:
                dropdown_links.insert(0, changelog_li + '<li><hr class="dropdown-divider"></li>')
            else:
                dropdown_links.append(changelog_li)
            break

        toggle_text = _("Toggle Dropdown")
        dropdown_button = format_html(
            '<button class="btn btn-sm btn-action dropdown-toggle dropdown-toggle-split" '
            'type="button" data-bs-toggle="dropdown" aria-expanded="false" aria-label="{}"></button>',
            toggle_text,
        )
        if button and dropdown_links:
            html += format_html(
                '<span class="btn-group dropdown">{}{}<ul class="dropdown-menu dropdown-menu-end">{}</ul></span>',
                mark_safe(button),
                dropdown_button,
                mark_safe("".join(dropdown_links)),
            )
        elif button:
            html += button
        elif dropdown_links:
            html += format_html(
                '<span class="btn-group dropdown">{}<ul class="dropdown-menu dropdown-menu-end">{}</ul></span>',
                dropdown_button,
                mark_safe("".join(dropdown_links)),
            )

        # Clone — a standalone leading button shown for any model that has a
        # clone view wired (i.e. uses CloneableMixin + a *_clone URL). Ghost
        # like edit: monochrome at rest, indigo on hover.
        clone_html = ""
        clone_viewname = get_model_viewname(model, "clone")
        for clone_kwargs in ({"pk": record.pk}, {"slug": getattr(record, "slug", None)}):
            if None in clone_kwargs.values():
                continue
            try:
                clone_url = reverse(clone_viewname, kwargs=clone_kwargs)
            except NoReverseMatch:
                continue
            clone_title = _("Clone")
            clone_html = (
                f'<a class="btn btn-sm btn-action me-1" href="{clone_url}" '
                f'title="{clone_title}" aria-label="{clone_title}">'
                f'<i class="mdi mdi-content-copy"></i></a>'
            )
            break

        return mark_safe(self.get_leading_buttons(record, table) + clone_html + html)

    def get_leading_buttons(self, record, table):
        """Hook for subclasses to prepend extra per-row buttons (e.g. a
        check-out button) ahead of the clone/edit/delete actions. Returns an
        HTML-safe string."""
        return ""


class AssigneeColumn(tables.Column):
    """
    A reusable column that resolves an AssetHolder (via AssetAssignment
    ForeignKey) for each row, falling back to a named model field
    (e.g., ``location``) when no holder is assigned.

    The bulk assignment lookup is cached on the parent table instance so
    that only a single database query is issued per table rendering.

    Usage::

        assignee = AssigneeColumn(location_field='location')

    Parameters:
        location_field (str | None): Model field name to display as fallback
            when no AssetHolder is assigned.
        empty_text (str): Displayed when no holder or location is available.
        assignment_model_path (str): Dotted app-model path to the
            AssetAssignment-like model.
    """

    def __init__(
        self,
        *args,
        location_field=None,
        empty_text=None,
        assignment_model_path="assets.AssetAssignment",
        **kwargs,
    ):
        kwargs.setdefault("verbose_name", _("Assignee"))
        kwargs.setdefault("orderable", False)
        kwargs.setdefault("accessor", "pk")
        self.location_field = location_field
        self._empty_text = empty_text
        self._assignment_model_path = assignment_model_path
        super().__init__(*args, **kwargs)

    def _get_assignment_model(self):

        return apps.get_model(self._assignment_model_path)

    def get_prefetch_fields(self):
        return [
            "assignments",
            "assignments__assigned_user",
            "assignments__assigned_location",
            "assignments__assigned_asset",
        ]

    def render(self, value, record, bound_column, table=None):
        if table is None:
            table = bound_column._table
        cache_attr = f"_assignee_cache_{id(self)}"
        if not hasattr(table, cache_attr):
            self._build_cache(table, record.__class__, cache_attr)
        cache = getattr(table, cache_attr)

        holder = cache.get(value)
        if holder:
            try:
                url = holder.get_absolute_url()
                if getattr(holder, "_meta", None) is not None and holder._meta.label_lower == "organization.location":
                    return format_html('{}: <a href="{}">{}</a>', _("Location"), url, holder)
                return format_html('<a href="{}">{}</a>', url, holder)
            # broad except: render-degrade: an assignee link may fail without breaking the table
            except Exception:
                return str(holder)

        if hasattr(record, "active_assignment") and record.active_assignment is None:
            return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)

        if self.location_field and hasattr(record, self.location_field):
            loc = getattr(record, self.location_field)
            if loc:
                try:
                    url = loc.get_absolute_url()
                    return format_html('{}: <a href="{}">{}</a>', _("Location"), url, loc)
                except Exception:  # broad except: render-degrade: location links may fail without breaking the table
                    return f"{_('Location')}: {loc}"

        if self._empty_text is not None:
            return self._empty_text
        return format_html('<span class="text-muted">{}</span>', TABLE_EMPTY_VALUE)

    def _build_cache(self, table, model_class, cache_attr):

        AssignmentModel = self._get_assignment_model()
        page = getattr(table, "page", None)
        # page.object_list is a BoundRows wrapper whose data is already sliced.
        # Iterating the wrapper would create new rows and advance table._counter.
        records = page.object_list.data if page is not None else table.data
        pks = [record.pk for record in records]
        if not pks:
            setattr(table, cache_attr, {})
            return

        cache = {}

        # Check if AssignmentModel has a direct ForeignKey to model_class
        fk_field = None
        for field in AssignmentModel._meta.fields:
            if field.is_relation and field.related_model == model_class:
                fk_field = field
                break

        if fk_field is not None:
            filter_kwargs = {f"{fk_field.name}_id__in": pks}
            if hasattr(AssignmentModel, "is_active"):
                filter_kwargs["is_active"] = True

            select_rels = []
            for f in AssignmentModel._meta.fields:
                if f.name in ("assigned_user", "assigned_location", "assigned_asset", "assigned_holder"):
                    select_rels.append(f.name)
            assignments = AssignmentModel.objects.filter(**filter_kwargs)
            if select_rels:
                assignments = assignments.select_related(*select_rels)
            elif hasattr(AssignmentModel, "assigned_to_content_type"):
                assignments = assignments.select_related("assigned_to_content_type")

            if hasattr(AssignmentModel, "assigned_to_content_type") and not select_rels:
                # Handle old GenericForeignKey case
                assigned_to_ids_by_ct = {}
                for a in assignments:
                    ct_id = getattr(a, "assigned_to_content_type_id", None)
                    if not ct_id:
                        continue
                    if ct_id not in assigned_to_ids_by_ct:
                        assigned_to_ids_by_ct[ct_id] = []
                    parent_id = getattr(a, f"{fk_field.name}_id")
                    assigned_to_ids_by_ct[ct_id].append((parent_id, a.assigned_to_object_id))

                ct_map = {}
                for ct_id in assigned_to_ids_by_ct:
                    ct = ContentType.objects.get_for_id(ct_id)
                    ct_map[ct_id] = ct

                for ct_id, entries in assigned_to_ids_by_ct.items():
                    ct = ct_map.get(ct_id)
                    if ct is None:
                        continue
                    model = ct.model_class()
                    if model is None:
                        continue
                    obj_ids = [e[1] for e in entries]
                    instances = model.objects.filter(pk__in=obj_ids)
                    instance_map = {obj.pk: obj for obj in instances}
                    for parent_id, obj_id in entries:
                        cache[parent_id] = instance_map.get(obj_id)
            else:
                # Handle explicit FKs case
                for a in assignments:
                    parent_id = getattr(a, f"{fk_field.name}_id")
                    target = getattr(a, "assigned_target", None)
                    if target is None:
                        target = (
                            getattr(a, "assigned_holder", None)
                            or getattr(a, "assigned_user", None)
                            or getattr(a, "assigned_location", None)
                            or getattr(a, "assigned_asset", None)
                        )
                    cache[parent_id] = target

        else:
            ct = ContentType.objects.get_for_model(model_class)
            assignments = AssignmentModel.objects.filter(content_type=ct, object_id__in=pks).select_related(
                "asset_holder"
            )
            cache = {a.object_id: a.asset_holder for a in assignments if getattr(a, "asset_holder", None)}

        setattr(table, cache_attr, cache)
