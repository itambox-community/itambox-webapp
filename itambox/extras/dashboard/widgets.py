import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from html import unescape
from types import SimpleNamespace

import markdown as md
from django import forms
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from assets.models import Asset, AssetMaintenance, AssetType, Manufacturer, StatusLabel
from core.context import override_current_tenant_scope
from core.forms import TenantScopedFormMixin
from core.managers import Scope
from core.models import ObjectChange
from core.tenant_access import accessible_tenant_ids, active_membership
from extras.models import Bookmark
from extras.utils import resolve_generic_items
from inventory.models import (
    Accessory,
    AccessoryAssignment,
    AccessoryStock,
    Component,
    ComponentAllocation,
    ComponentStock,
    Consumable,
    ConsumableAssignment,
    ConsumableStock,
)
from licenses.models import License
from organization.models import Location, Site, Tenant
from software.models import Software
from subscriptions.models import Subscription

# -----------------------------------------------------------------------------
# Widget Registry
# -----------------------------------------------------------------------------

_registry = {}


def register_widget(cls):
    """Register a DashboardWidget subclass."""
    _registry[cls.widget_id] = cls
    return cls


def get_widget(widget_id):
    """Get a registered widget class by its widget_id."""
    return _registry.get(widget_id)


def get_registered_widgets():
    """Return all registered widget classes."""
    return list(_registry.values())


# -----------------------------------------------------------------------------
# Tenant scoping & multitenancy helper
# -----------------------------------------------------------------------------


_TARGET_SCOPE_ATTR = "_dashboard_target_scope"
_MISSING = object()


def dashboard_target_tenants(user):
    """Return every live tenant the user may target from a dashboard."""
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return Tenant._base_manager.none()
    if getattr(user, "is_superuser", False):
        return Tenant._base_manager.filter(deleted_at__isnull=True).order_by("name", "pk")
    return Tenant._base_manager.filter(
        pk__in=accessible_tenant_ids(user),
        deleted_at__isnull=True,
    ).order_by("name", "pk")


def _coerce_target_tenant_id(raw_tenant_id):
    if raw_tenant_id is None or raw_tenant_id == "" or isinstance(raw_tenant_id, bool):
        return None
    if isinstance(raw_tenant_id, int):
        return raw_tenant_id
    if isinstance(raw_tenant_id, str) and raw_tenant_id.isdecimal():
        return int(raw_tenant_id)
    return None


def resolve_dashboard_target_tenant(user, raw_tenant_id):
    """Return one authorized live target, or ``None`` without disclosing it."""
    tenant_id = _coerce_target_tenant_id(raw_tenant_id)
    if tenant_id is None:
        return None
    return dashboard_target_tenants(user).filter(pk=tenant_id).first()


def _target_tenant_value(config):
    if not isinstance(config, dict):
        return False, None
    nested_config = config.get("config")
    if isinstance(nested_config, dict) and "tenant_id" in nested_config:
        raw_tenant_id = nested_config["tenant_id"]
    elif "tenant_id" in config:
        raw_tenant_id = config["tenant_id"]
    else:
        return False, None
    return raw_tenant_id not in (None, ""), raw_tenant_id


def _resolve_target_tenant(config, user=None):
    """Resolve a configured target using the canonical dashboard access policy."""
    requested, raw_tenant_id = _target_tenant_value(config)
    if not requested:
        return None
    return resolve_dashboard_target_tenant(user, raw_tenant_id)


def _request_target_scope(request, config):
    requested, raw_tenant_id = _target_tenant_value(config)
    if not requested:
        return False, None
    marker = getattr(request, _TARGET_SCOPE_ATTR, _MISSING)
    if marker is not _MISSING:
        return True, marker[0]
    return True, resolve_dashboard_target_tenant(getattr(request, "user", None), raw_tenant_id)


@contextmanager
def _dashboard_render_scope(request, target):
    """Overlay target tenant state for one widget render and restore it exactly."""
    attr_names = ("active_tenant", "active_tenant_group", "active_membership", "active_all_accessible")
    previous_attrs = {
        name: (True, getattr(request, name)) if hasattr(request, name) else (False, None) for name in attr_names
    }
    previous_marker = (
        (True, getattr(request, _TARGET_SCOPE_ATTR)) if hasattr(request, _TARGET_SCOPE_ATTR) else (False, None)
    )
    membership = active_membership(request.user, target.pk)
    with override_current_tenant_scope(target, membership):
        try:
            request.active_tenant = target
            request.active_tenant_group = None
            request.active_membership = membership
            request.active_all_accessible = False
            setattr(request, _TARGET_SCOPE_ATTR, (target, membership))
            yield
        finally:
            if previous_marker[0]:
                setattr(request, _TARGET_SCOPE_ATTR, previous_marker[1])
            else:
                delattr(request, _TARGET_SCOPE_ATTR)
            for name, (present, value) in previous_attrs.items():
                if present:
                    setattr(request, name, value)
                else:
                    delattr(request, name)


def _scoped_all(model_class):
    """Return ``objects.all()``, bound to the explicit ``Scope`` for explicit-scope managers."""
    qs = model_class.objects.all()
    if hasattr(qs, "for_scope"):
        return qs.for_scope(Scope.current())
    return qs


def get_scoped_queryset(model_class, request, config=None):
    """Return rows under the request scope and an authorized explicit target."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return model_class.objects.none()

    requested, target = _request_target_scope(request, config)
    if not requested:
        return _scoped_all(model_class)
    if target is None:
        return model_class.objects.none()

    marker = getattr(request, _TARGET_SCOPE_ATTR, _MISSING)
    if marker is not _MISSING and marker[0] is target:
        qs = _scoped_all(model_class)
        return _narrow_queryset_to_tenant(qs, model_class, target.pk)

    with override_current_tenant_scope(target, active_membership(user, target.pk)):
        qs = _scoped_all(model_class)
        return _narrow_queryset_to_tenant(qs, model_class, target.pk)


def _narrow_queryset_to_tenant(qs, model_class, tenant_id):
    """Intersect a canonical queryset with one authorized tenant."""
    try:
        model_class._meta.get_field("tenant")
        return qs.filter(tenant_id=tenant_id)
    except FieldDoesNotExist:
        pass
    tenant_lookup = getattr(model_class, "tenant_lookup", None)
    if tenant_lookup:
        return qs.filter(**{f"{tenant_lookup}_id": tenant_id})
    return qs


def internal_change_noise_q():
    """Return the condition selecting change-log rows the dashboard can hide.

    The changelog widget records the objects that were changed, so the entries
    that drown human activity are background-job bookkeeping (``core.job``),
    changes with no acting user (system-generated: alert engine, expiry sweeps),
    and the legacy ``extras.event`` exclusion kept for compatibility.

    Returning one composable ``Q`` keeps the widget's optional action and
    object-type filters composing with the exclusion instead of replacing it.
    """
    return (
        Q(changed_object_type__app_label="extras", changed_object_type__model="event")
        | Q(changed_object_type__app_label="core", changed_object_type__model="job")
        | Q(user__isnull=True)
    )


# --- Proxy Wrappers for Inventory ---


class ScopedAccessoryWrapper:
    """Wraps an Accessory model instance, exposing the scoped available quantity."""

    def __init__(self, accessory, available):
        self.accessory = accessory
        self._available = available

    def __getattr__(self, name):
        return getattr(self.accessory, name)

    @property
    def available(self):
        return self._available


class ScopedConsumableWrapper:
    """Wraps a Consumable model instance, exposing the scoped available quantity."""

    def __init__(self, consumable, available):
        self.consumable = consumable
        self._available = available

    def __getattr__(self, name):
        return getattr(self.consumable, name)

    @property
    def available(self):
        return self._available


# -----------------------------------------------------------------------------
# Base Widget
# -----------------------------------------------------------------------------


class WidgetConfigForm(TenantScopedFormMixin, forms.Form):
    pass


class DashboardWidget:
    widget_id = None  # Unique string ID (set in subclasses)
    title = ""  # Default display title
    description = ""  # Short description shown in add-widget modal
    template_name = None  # Template for widget body content
    icon = "view-grid-outline"  # MDI icon name (without 'mdi-' prefix) for the header chip
    admin_only = False  # Restricted to global administrators
    required_target_permissions = ()

    def __init__(self, config=None):
        self.config = config or {}

    class ConfigForm(WidgetConfigForm):
        pass

    def get_config_value(self, key, default=None):
        cfg = self.config.get("config", {}) if isinstance(self.config, dict) else {}
        return cfg.get(key, default)

    def get_config_form(self, data=None, request=None):
        cls = type(self).ConfigForm
        initial = self.config.get("config", {}) if isinstance(self.config, dict) else {}
        form = cls(data=data, initial=initial or {})

        # If request is provided and user is staff/superuser, dynamically inject a 'tenant_id' field
        if request and (request.user.is_superuser or (hasattr(request.user, "is_staff") and request.user.is_staff)):
            if self.widget_id != "tenant-spend":  # exclude tenant-spend widget which is global
                tenants = dashboard_target_tenants(request.user)
                choices = [("", _("All Tenants"))] + [(str(t.id), t.name) for t in tenants]
                form.fields["tenant_id"] = forms.ChoiceField(
                    label=_("Target Tenant Context"),
                    choices=choices,
                    required=False,
                    initial=initial.get("tenant_id", ""),
                    widget=forms.Select(attrs={"class": "form-select"}),
                    help_text=_("Scope this widget to a specific tenant."),
                )
        return form

    def has_permission(self, request):
        if self.admin_only:
            user = request.user
            return user.is_superuser or (hasattr(user, "is_staff") and user.is_staff)
        return True

    def get_required_target_permissions(self):
        return tuple(self.required_target_permissions)

    def has_target_permissions(self, request, target):
        return all(
            request.user.has_perm(permission, obj=target) for permission in self.get_required_target_permissions()
        )

    @property
    def display_title(self):
        return self.config.get("title") or self.title

    def get_template_name(self):
        return self.template_name

    def get_context(self, request):
        """Return a dict of context to render the widget template. Override in subclasses."""
        return {}

    def render(self, request):
        """Render the widget body HTML under any authorized explicit target."""
        if not self.has_permission(request):
            return mark_safe(
                f'<div class="text-danger text-center py-4">{_("Restricted to Global Administrators.")}</div>'
            )

        requested, target = _request_target_scope(request, self.config)
        if requested and target is None:
            return ""
        if requested and not self.has_target_permissions(request, target):
            return ""

        def render_body():
            ctx = self.get_context(request)
            ctx["widget"] = self
            return render_to_string(self.get_template_name(), ctx, request=request)

        if requested:
            with _dashboard_render_scope(request, target):
                return render_body()
        return render_body()

    def get_footer_links(self, request):
        """Return a list of dictionaries with 'url' and 'label' for card footer buttons."""
        return []


# -----------------------------------------------------------------------------
# Widget Subclasses (one per dashboard card)
# -----------------------------------------------------------------------------


@register_widget
class NoteWidget(DashboardWidget):
    widget_id = "note"
    icon = "note-text-outline"
    title = _lazy("Note")
    description = _lazy("Display arbitrary custom content. Markdown is supported.")
    template_name = "extras/dashboard/widgets/note.html"

    class ConfigForm(WidgetConfigForm):
        content = forms.CharField(
            label=_("Content"),
            widget=forms.Textarea(attrs={"class": "form-control", "rows": 6}),
            required=False,
            help_text=_("Markdown is supported (headings, bold, lists, links, tables, code)."),
        )

    def get_context(self, request):
        raw = self.get_config_value("content", "")
        content_html = ""
        if raw:
            # Escape FIRST so user-supplied raw HTML/scripts are neutralized,
            # then let markdown add formatting on top of the escaped text.
            # (Tradeoff: the blockquote '>' prefix is escaped away — acceptable
            # in exchange for XSS safety without a sanitizer dependency.)
            rendered = md.markdown(
                escape(raw),
                extensions=["extra", "sane_lists", "nl2br"],
            )

            # escape() does not touch markdown link syntax, so [x](javascript:…)/[y](data:…)
            # still emit a dangerous href/src. Neutralize those schemes to '#'.
            #
            # Bypass hardening: browsers strip ASCII control characters (tabs,
            # newlines, \x00-\x1f) from inside a URL scheme before evaluating
            # it, so "java\tscript:" is treated as "javascript:" by the browser
            # even though the raw pattern match would miss it. Additionally,
            # HTML entities (e.g. &#106;avascript:) can encode scheme chars.
            # Mitigation: decode HTML entities and strip ASCII control characters
            # from each URL value before scheme-checking, then re-block on the
            # normalised form.
            def _neutralize_url(m):
                attr_prefix = m.group(1)  # e.g. 'href="'
                url_value = m.group(2)  # everything up to the closing '"'
                # 1. Decode HTML entities that may encode scheme characters.
                decoded = unescape(url_value)
                # 2. Strip ASCII control characters (\x00–\x1f) from the URL
                #    so "java\x09script:" normalises to "javascript:".
                stripped = re.sub(r"[\x00-\x1f]", "", decoded)
                # 3. Check the normalised scheme against the blocklist.
                if re.match(r"\s*(?:javascript|data|vbscript)\s*:", stripped, re.IGNORECASE):
                    return attr_prefix + '#"'
                return m.group(0)

            rendered = re.sub(
                r'((?:href|src)\s*=\s*")([^"]*)"',
                _neutralize_url,
                rendered,
                flags=re.IGNORECASE,
            )
            content_html = mark_safe(rendered)
        return {
            "content": raw,
            "content_html": content_html,
        }


OBJECT_COUNT_MODEL_CHOICES = [
    ("assets.asset", _lazy("Assets")),
    ("assets.assettype", _lazy("Asset Types")),
    ("assets.manufacturer", _lazy("Manufacturers")),
    ("assets.statuslabel", _lazy("Status Labels")),
    ("inventory.component", _lazy("Components")),
    ("inventory.accessory", _lazy("Accessories")),
    ("inventory.consumable", _lazy("Consumables")),
    ("organization.site", _lazy("Sites")),
    ("organization.tenant", _lazy("Tenants")),
    ("organization.location", _lazy("Locations")),
    ("licenses.license", _lazy("Licenses")),
    ("subscriptions.subscription", _lazy("Subscriptions")),
    ("software.software", _lazy("Software")),
]


@register_widget
class ObjectCountsWidget(DashboardWidget):
    widget_id = "object-counts"
    required_target_permissions = ()
    icon = "counter"
    title = _lazy("Object Counts")
    description = _lazy("Display counts of object types with links to their list views.")
    template_name = "extras/dashboard/widgets/object_counts.html"

    class ConfigForm(WidgetConfigForm):
        # NOTE: the former 'display_style' option was dropped — the widget now
        # always renders the quiet label/number row list. Stale values in
        # saved configs are simply ignored.
        models = forms.MultipleChoiceField(
            label=_("Models"),
            choices=OBJECT_COUNT_MODEL_CHOICES,
            widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
            required=False,
        )

    def get_required_target_permissions(self):
        selected = self.get_config_value("models", [])
        return tuple(
            f"{app_label}.view_{model_name}"
            for app_label, model_name in (key.split(".", 1) for key in selected if "." in key)
        )

    def _get_model_label(self, key):
        for k, label in OBJECT_COUNT_MODEL_CHOICES:
            if k == key:
                return label
        return key

    def get_context(self, request):
        selected = self.get_config_value("models", [])
        if not selected:
            return {"counts": [], "has_data": False}

        model_map = {
            "assets.asset": (Asset, "assets:asset_list"),
            "assets.assettype": (AssetType, "assets:assettype_list"),
            "assets.manufacturer": (Manufacturer, "assets:manufacturer_list"),
            "assets.statuslabel": (StatusLabel, "assets:statuslabel_list"),
            "inventory.component": (Component, "inventory:component_list"),
            "inventory.accessory": (Accessory, "inventory:accessory_list"),
            "inventory.consumable": (Consumable, "inventory:consumable_list"),
            "organization.site": (Site, "organization:site_list"),
            "organization.tenant": (Tenant, "organization:tenant_list"),
            "organization.location": (Location, "organization:location_list"),
            "licenses.license": (License, "licenses:license_list"),
            "subscriptions.subscription": (Subscription, "subscriptions:subscription_list"),
            "software.software": (Software, "software:software_list"),
        }
        counts = []
        for key in selected:
            info = model_map.get(key)
            if info is None:
                continue
            model_cls, url_name = info
            try:
                scoped_qs = get_scoped_queryset(model_cls, request, config=self.config.get("config", {}))
                count = scoped_qs.count()
                label = self._get_model_label(key)
                url = reverse(url_name)
                counts.append({"label": label, "count": count, "url": url})
            except Exception:
                counts.append({"label": self._get_model_label(key), "count": "?", "url": None})
        return {
            "counts": counts,
            "has_data": True,
        }


@register_widget
class FinancialWidget(DashboardWidget):
    widget_id = "financial-overview"
    required_target_permissions = ("assets.view_asset", "assets.view_assetmaintenance")
    icon = "wallet-outline"
    title = _lazy("Financial Overview")
    description = _lazy("Total cost of ownership, purchase costs, maintenance, and salvage values")
    template_name = "extras/dashboard/widgets/financial.html"

    METRIC_CHOICES = [
        ("purchase", _lazy("Original Purchase Cost")),
        ("maintenance", _lazy("Maintenance Expenditures")),
        ("salvage", _lazy("Total Salvage Book Value")),
        ("asset_count", _lazy("Costed Asset Count")),
    ]

    # NOTE: the former per-widget 'currency' symbol option was dropped —
    # formatting now goes through the `money` template filter, which derives
    # currency (symbol, placement, separators) from the tenant /
    # ITAMBOX_DEFAULT_CURRENCY. Stale saved values are ignored.
    class ConfigForm(WidgetConfigForm):
        budget_limit = forms.DecimalField(
            label=_("Budget Limit"),
            max_digits=12,
            decimal_places=2,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
            help_text=_("If set, a budget utilization bar compares TCO against this limit."),
        )
        metrics = forms.MultipleChoiceField(
            label=_("Metrics to Display"),
            choices=[
                ("purchase", _lazy("Original Purchase Cost")),
                ("maintenance", _lazy("Maintenance Expenditures")),
                ("salvage", _lazy("Total Salvage Book Value")),
                ("asset_count", _lazy("Costed Asset Count")),
            ],
            widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
            required=False,
            help_text=_("Rows shown below the TCO headline. If none selected, all are shown."),
        )

    def get_context(self, request):
        assets = get_scoped_queryset(Asset, request, config=self.config.get("config", {}))
        maintenances = get_scoped_queryset(AssetMaintenance, request, config=self.config.get("config", {}))

        # Resolve the scoped tenant (mirrors RenewalsWidget). Used both as the
        # currency fallback for blank-currency records and to pick the budget
        # bucket below.
        tenant = None
        tenant_id = self.get_config_value("tenant_id") or self.config.get("tenant_id")
        if tenant_id:
            tenant = Tenant.objects.for_scope(Scope.current()).filter(id=tenant_id).first()
        if tenant is None:
            tenant = getattr(request, "active_tenant", None)

        # The concrete ISO code blank-currency records fall back to (tenant
        # currency, else ITAMBOX_DEFAULT_CURRENCY) — matches the `money` filter's
        # resolution. Records sharing this fallback code are folded into one
        # bucket so a single-currency install shows exactly one figure.
        default_code = (
            (getattr(tenant, "currency", None) or "") or getattr(settings, "ITAMBOX_DEFAULT_CURRENCY", "EUR") or "EUR"
        ).upper()

        def _bucket_key(raw_currency):
            return (raw_currency or "").upper() or default_code

        # Per-currency asset sums: purchase_cost + salvage_value carry the
        # asset's single `currency` field. Maintenance cost carries its own.
        # Costs are NOT summed across currencies (no FX source); group by code.
        purchase_by_cur = {}  # code -> float purchase total
        salvage_by_cur = {}  # code -> float salvage total
        costed_count = 0
        for row in assets.values("currency").annotate(
            purchase_total=Sum("purchase_cost"),
            salvage_total=Sum("salvage_value"),
            row_costed=Count("pk", filter=Q(purchase_cost__isnull=False)),
        ):
            code = _bucket_key(row["currency"])
            purchase_by_cur[code] = purchase_by_cur.get(code, 0.0) + float(row["purchase_total"] or 0.0)
            salvage_by_cur[code] = salvage_by_cur.get(code, 0.0) + float(row["salvage_total"] or 0.0)
            costed_count += row["row_costed"] or 0

        maintenance_by_cur = {}  # code -> float maintenance total
        for row in maintenances.values("currency").annotate(total=Sum("cost")):
            code = _bucket_key(row["currency"])
            maintenance_by_cur[code] = maintenance_by_cur.get(code, 0.0) + float(row["total"] or 0.0)

        # TCO per currency = purchase + maintenance (salvage is informational,
        # shown as its own metric row, never folded into TCO — matches the
        # previous single-total behaviour: total_tco = purchase + maintenance).
        all_codes = set(purchase_by_cur) | set(salvage_by_cur) | set(maintenance_by_cur)
        currency_breakdown = []
        for code in all_codes:
            purchase = purchase_by_cur.get(code, 0.0)
            maintenance = maintenance_by_cur.get(code, 0.0)
            salvage = salvage_by_cur.get(code, 0.0)
            # Per-bucket currency context for the `money` filter: an explicit
            # ISO code (never blank — blanks were folded into default_code).
            currency_obj = SimpleNamespace(currency=code, tenant=tenant)
            currency_breakdown.append(
                {
                    "currency_code": code,
                    "currency_obj": currency_obj,
                    "total_tco": purchase + maintenance,
                    "total_purchase_cost": purchase,
                    "total_maintenance_cost": maintenance,
                    "total_salvage_value": salvage,
                }
            )
        currency_breakdown.sort(key=lambda b: b["total_tco"], reverse=True)

        # Budget bar: budget_limit is single-currency. Compare ONLY the TCO
        # bucket whose currency matches the budget's currency (the tenant /
        # config currency, == default_code). Other-currency TCO figures are
        # listed separately. If the budget currency is ambiguous we fall back to
        # the tenant currency (default_code) and flag it for the template.
        budget_limit = self.get_config_value("budget_limit")
        budget_pct = None
        budget_exceeded = False
        budget_currency_obj = SimpleNamespace(currency=default_code, tenant=tenant)
        budget_tco = next(
            (b["total_tco"] for b in currency_breakdown if b["currency_code"] == default_code),
            0.0,
        )
        # Buckets in any other currency than the budget's.
        other_currency_breakdown = [b for b in currency_breakdown if b["currency_code"] != default_code]
        if budget_limit:
            budget_limit = float(budget_limit)
            budget_pct = min(100, round(budget_tco / budget_limit * 100)) if budget_limit > 0 else 100
            budget_exceeded = budget_tco > budget_limit

        metrics = self.get_config_value("metrics") or [k for k, _label in self.METRIC_CHOICES]

        return {
            "currency_breakdown": currency_breakdown,
            "other_currency_breakdown": other_currency_breakdown,
            "costed_asset_count": costed_count,
            "budget_limit": budget_limit,
            "budget_pct": budget_pct,
            "budget_exceeded": budget_exceeded,
            "budget_tco": budget_tco,
            "budget_currency_code": default_code,
            "budget_currency_obj": budget_currency_obj,
            # True when more than one currency exists, so the budget bar only
            # reflects one of them (the template can note this).
            "budget_currency_ambiguous": bool(other_currency_breakdown),
            "metrics": metrics,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("assets:asset_list"), "label": _("View Cost Details")}]


@register_widget
class StatusLabelsWidget(DashboardWidget):
    widget_id = "status-labels"
    required_target_permissions = ("assets.view_asset",)
    icon = "label-multiple-outline"
    title = _lazy("Asset Status Labels")
    description = _lazy("Donut chart showing asset distribution by status label")
    template_name = "extras/dashboard/widgets/status_labels.html"

    class ConfigForm(WidgetConfigForm):
        chart_type = forms.ChoiceField(
            label=_("Chart Type"),
            choices=[
                ("doughnut", _lazy("Doughnut")),
                ("pie", _lazy("Pie")),
                ("bar", _lazy("Bar")),
                ("list", _lazy("Simple List")),
            ],
            initial="doughnut",
            widget=forms.Select(attrs={"class": "form-select"}),
            required=False,
        )

    def get_context(self, request):
        # Count exactly the assets visible under the canonical scope (single
        # tenant / group / all-accessible / superuser-global), narrowed by any
        # per-widget or tenant-bound-dashboard target. StatusLabel is a global
        # catalogue table, so the scoping rides entirely on the Asset subquery
        # rather than a second, divergent active-tenant/AssetHolder resolution.
        scoped_assets = get_scoped_queryset(Asset, request, config=self.config.get("config", {}))
        statuses = StatusLabel.objects.annotate(
            asset_count=Count("assets", filter=Q(assets__in=scoped_assets))
        ).order_by("-asset_count")
        total_assets = scoped_assets.count()

        chart_data = []
        for status in statuses:
            if status.asset_count > 0:
                chart_data.append(
                    {
                        "name": status.name,
                        "count": status.asset_count,
                        "color": f"#{status.color}" if status.color else "#626976",
                    }
                )

        return {
            "total_assets": total_assets,
            "status_stats": statuses,
            "chart_type": self.get_config_value("chart_type", "doughnut"),
            "chart_data_json": json.dumps(chart_data),
        }

    def get_footer_links(self, request):
        return [{"url": reverse("assets:asset_list"), "label": _("View All Assets")}]


@register_widget
class LicenseWidget(DashboardWidget):
    widget_id = "license-utilization"
    required_target_permissions = ("licenses.view_license",)
    icon = "certificate-outline"
    title = _lazy("Software License Seats")
    description = _lazy("Top 5 licenses by seat utilization percentage")
    template_name = "extras/dashboard/widgets/licenses.html"

    class ConfigForm(WidgetConfigForm):
        limit = forms.IntegerField(
            label=_("Limit to Top N"),
            initial=5,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )
        warning_threshold = forms.DecimalField(
            label=_("Warning Threshold (%)"),
            initial=85.0,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
            help_text=_("Threshold percentage to flag high utilization."),
        )

    def get_context(self, request):
        stats = []
        licenses = get_scoped_queryset(License, request, config=self.config.get("config", {}))
        for lic in licenses.with_counts():
            total = lic.seats
            allocated = lic.assigned_count
            pct = round((allocated / total) * 100) if total > 0 else 0
            stats.append({"license": lic, "total": total, "allocated": allocated, "util_pct": pct})
        stats.sort(key=lambda x: x["util_pct"], reverse=True)

        limit = self.get_config_value("limit", 5)
        warning_threshold = float(self.get_config_value("warning_threshold", 85.0))

        return {
            "license_stats": stats[:limit],
            "warning_threshold": warning_threshold,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("licenses:license_list"), "label": _("View All Licenses")}]


@register_widget
class MaintenanceWidget(DashboardWidget):
    widget_id = "active-maintenances"
    required_target_permissions = ("assets.view_assetmaintenance", "assets.view_asset")
    icon = "wrench-outline"
    title = _lazy("Active Repairs & Maintenances")
    description = _lazy("Ongoing repairs and maintenance tasks with associated costs")
    template_name = "extras/dashboard/widgets/maintenances.html"

    class ConfigForm(WidgetConfigForm):
        limit = forms.IntegerField(
            label=_("Limit to Top N"),
            initial=10,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )
        highlight_overdue = forms.BooleanField(
            label=_("Highlight Long-Running Repairs"),
            initial=True,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
            help_text=_("Flag repairs running longer than the threshold below."),
        )
        overdue_days = forms.IntegerField(
            label=_("Long-Running Threshold (days)"),
            initial=30,
            min_value=1,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )

    def get_context(self, request):
        limit = self.get_config_value("limit", 10)
        highlight_overdue = self.get_config_value("highlight_overdue", True)
        overdue_days = int(self.get_config_value("overdue_days") or 30)
        today = date.today()

        maintenances = (
            get_scoped_queryset(AssetMaintenance, request, config=self.config.get("config", {}))
            .filter(completion_date__isnull=True)
            .select_related("asset")
            .order_by("-start_date")
        )

        items = list(maintenances[:limit])
        for maint in items:
            maint.days_running = (today - maint.start_date).days if maint.start_date else 0
            maint.is_overdue = highlight_overdue and maint.days_running > overdue_days

        return {
            "active_maintenances": items,
            "active_maintenance_count": maintenances.count(),
            "highlight_overdue": highlight_overdue,
            "overdue_days": overdue_days,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("assets:assetmaintenance_list"), "label": _("View All Repairs")}]


@register_widget
class EOLAlertsWidget(DashboardWidget):
    widget_id = "eol-alerts"
    required_target_permissions = ("assets.view_asset", "assets.view_assettype")
    icon = "calendar-alert"
    title = _lazy("EOL Planning Alerts")
    description = _lazy("Hardware expiring within 90 days or already past EOL")
    template_name = "extras/dashboard/widgets/eol_alerts.html"

    class ConfigForm(WidgetConfigForm):
        days_horizon = forms.ChoiceField(
            label=_("Planning Horizon"),
            choices=[
                ("30", _lazy("30 Days")),
                ("90", _lazy("90 Days (Default)")),
                ("180", _lazy("180 Days")),
                ("365", _lazy("365 Days")),
            ],
            initial="90",
            widget=forms.Select(attrs={"class": "form-select"}),
            required=False,
        )

    def get_context(self, request):
        today = date.today()
        days_horizon = int(self.get_config_value("days_horizon", 90))
        alerts = []

        # 1. Resolve distinct EOL months in the catalog

        eol_month_values = list(
            AssetType.objects.filter(eol_months__isnull=False).values_list("eol_months", flat=True).distinct()
        )

        if not eol_month_values:
            return {"eol_alerts": [], "days_horizon": days_horizon}

        # 2. Build pre-filtering query to filter only assets approaching EOL in the database
        q_filter = Q()
        for M in eol_month_values:
            # purchase_date + M months <= today + days_horizon
            cutoff_date = today + timedelta(days=days_horizon) - timedelta(days=int(M * 30.44))
            q_filter |= Q(asset_type__eol_months=M, purchase_date__lte=cutoff_date)

        queryset = (
            get_scoped_queryset(Asset, request, config=self.config.get("config", {}))
            .filter(purchase_date__isnull=False, asset_type__eol_months__isnull=False)
            .filter(q_filter)
            .select_related("asset_type", "asset_type__manufacturer")
        )

        for asset in queryset:
            eol = asset.eol_date
            if eol is None:
                continue
            days_left = (eol - today).days
            if days_left > days_horizon:
                continue
            alerts.append({"asset": asset, "days_left": days_left, "eol_date": eol})

        return {
            "eol_alerts": sorted(alerts, key=lambda a: a["days_left"]),
            "days_horizon": days_horizon,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("assets:asset_list"), "label": _("View All Assets")}]


@register_widget
class ChangelogWidget(DashboardWidget):
    widget_id = "recent-activity"
    required_target_permissions = ("core.view_objectchange",)
    icon = "history"
    title = _lazy("Change Log")
    description = _lazy("Recent object changes across the system (create, update, delete)")
    template_name = "extras/dashboard/widgets/activity.html"

    class ConfigForm(WidgetConfigForm):
        limit = forms.IntegerField(
            label=_("Max Items to Display"),
            initial=10,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )
        action_filter = forms.ChoiceField(
            label=_("Filter by Action"),
            choices=[
                ("", _lazy("All Actions")),
                ("create", _lazy("Creations Only")),
                ("update", _lazy("Updates Only")),
                ("delete", _lazy("Deletions Only")),
            ],
            initial="",
            widget=forms.Select(attrs={"class": "form-select"}),
            required=False,
        )
        models = forms.MultipleChoiceField(
            label=_("Filter by Object Types"),
            choices=OBJECT_COUNT_MODEL_CHOICES,
            widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
            required=False,
            help_text=_("If none selected, changes for all object types will be shown."),
        )
        hide_event_noise = forms.BooleanField(
            label=_("Hide Internal Event Entries"),
            initial=True,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
            help_text=_("Hide system-generated entries and background job updates so the feed shows human activity."),
        )

    def get_context(self, request):

        limit = self.get_config_value("limit", 10)
        action = self.get_config_value("action_filter", "")
        selected_models = self.get_config_value("models", [])

        qs = get_scoped_queryset(ObjectChange, request, config=self.config.get("config", {}))

        if self.get_config_value("hide_event_noise", True):
            qs = qs.exclude(internal_change_noise_q())

        if action:
            qs = qs.filter(action=action)

        if selected_models:
            ct_filters = Q()
            for m_label in selected_models:
                try:
                    app_label, model_name = m_label.split(".")
                    ct_filters |= Q(changed_object_type__app_label=app_label, changed_object_type__model=model_name)
                except ValueError:
                    continue
            qs = qs.filter(ct_filters)

        changes = qs.select_related("user", "changed_object_type").prefetch_related("changed_object")[:limit]

        return {
            "recent_changes": changes,
            "action_filter": action,
            "limit": limit,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("objectchange_list"), "label": _("View Full Change Log")}]


@register_widget
class RenewalsWidget(DashboardWidget):
    widget_id = "upcoming-renewals"
    required_target_permissions = ("subscriptions.view_subscription",)
    icon = "autorenew"
    title = _lazy("Upcoming Renewals")
    description = _lazy("Active subscriptions renewing within 90 days")
    template_name = "extras/dashboard/widgets/renewals.html"

    class ConfigForm(WidgetConfigForm):
        days_horizon = forms.ChoiceField(
            label=_("Planning Horizon"),
            choices=[
                ("30", _lazy("30 Days")),
                ("60", _lazy("60 Days")),
                ("90", _lazy("90 Days (Default)")),
                ("180", _lazy("180 Days")),
            ],
            initial="90",
            widget=forms.Select(attrs={"class": "form-select"}),
            required=False,
        )
        limit = forms.IntegerField(
            label=_("Max Items to Display"),
            initial=10,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )

    def get_context(self, request):
        today = date.today()
        days_horizon = int(self.get_config_value("days_horizon", 90))
        limit = self.get_config_value("limit", 10)
        cutoff = today + timedelta(days=days_horizon)

        # Resolve the scoped tenant for currency fallback (mirrors FinancialWidget).
        tenant = None
        tenant_id = self.get_config_value("tenant_id") or self.config.get("tenant_id")
        if tenant_id:
            tenant = Tenant.objects.for_scope(Scope.current()).filter(id=tenant_id).first()
        if tenant is None:
            tenant = getattr(request, "active_tenant", None)

        scoped_subs = get_scoped_queryset(Subscription, request, config=self.config.get("config", {}))
        subs = (
            scoped_subs.filter(
                status="active",
                renewal_date__isnull=False,
                renewal_date__lte=cutoff,
                renewal_date__gte=today,
            )
            .select_related("supplier", "tenant")
            .order_by("renewal_date")[:limit]
        )

        result = []
        for sub in subs:
            result.append(
                {
                    "pk": sub.pk,
                    "name": sub.name,
                    "supplier": sub.supplier,
                    "days_until_renewal": (sub.renewal_date - today).days,
                    "renewal_cost": sub.renewal_cost,
                    "currency": sub.currency,
                    # Per-row currency context for the `money` filter: the row's own
                    # currency (blank => the subscription's tenant => default).
                    "currency_obj": SimpleNamespace(currency=sub.currency or "", tenant=sub.tenant),
                }
            )

        # Active-subscription spend must NOT be summed across currencies — there
        # is no FX source. Group by the per-record currency (blank => the
        # scoped tenant's currency at display time) and emit one total per
        # currency; the template formats each via the `money` filter.
        spend_rows = (
            scoped_subs.filter(status="active")
            .values("currency")
            .annotate(total=Sum("renewal_cost"))
            .order_by("-total")
        )
        currency_spend = []
        for r in spend_rows:
            if r["total"] is None:
                continue
            # Wrap the row's currency so `{{ total|money:currency_obj }}` resolves
            # it (blank currency_obj.currency falls back to the tenant currency).
            currency_obj = SimpleNamespace(currency=r["currency"] or "", tenant=tenant)
            currency_spend.append({"total": r["total"], "currency_obj": currency_obj})

        return {
            "upcoming_renewals": result,
            "currency_spend": currency_spend,
            "days_horizon": days_horizon,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("subscriptions:subscription_list"), "label": _("View All Subscriptions")}]


@register_widget
class LowStockWidget(DashboardWidget):
    widget_id = "low-stock"
    icon = "package-variant-closed"
    title = _lazy("Low Stock Alerts")
    description = _lazy("Accessories, consumables, and components below minimum quantity")
    template_name = "extras/dashboard/widgets/low_stock.html"

    class ConfigForm(WidgetConfigForm):
        include_accessories = forms.BooleanField(
            label=_("Include Accessories"),
            initial=True,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
        )
        include_consumables = forms.BooleanField(
            label=_("Include Consumables"),
            initial=True,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
        )
        include_components = forms.BooleanField(
            label=_("Include Components"),
            initial=True,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
        )

    def get_required_target_permissions(self):
        permissions = []
        for key, permission in (
            ("include_accessories", "inventory.view_accessory"),
            ("include_consumables", "inventory.view_consumable"),
            ("include_components", "inventory.view_component"),
        ):
            if self.get_config_value(key, True):
                permissions.append(permission)
        return tuple(permissions)

    def get_context(self, request):
        # Item querysets are scoped by the canonical manager (get_scoped_queryset
        # for accessories/consumables; Component.objects for components). The
        # correlated stock/assignment/allocation subqueries each carry their OWN
        # tenant column, so they are scoped the same way via each model's
        # tenant-scoping manager (`.objects`). An explicit per-widget /
        # tenant-bound-dashboard target additionally narrows both sides to that
        # one tenant; there is no active-tenant or AssetHolder fallback.
        # The per-item-type work lives in helpers so this stays simple.
        requested, target = _request_target_scope(request, self.config)
        if requested and target is None:
            return {
                "low_stock_accessories": [],
                "low_stock_consumables": [],
                "low_stock_components": [],
                "low_stock_accessory_count": 0,
                "low_stock_consumable_count": 0,
                "low_stock_component_count": 0,
            }
        target_id = target.pk if target is not None else None

        low_accessories = (
            self._low_stock_accessories(request, target_id)
            if self.get_config_value("include_accessories", True)
            else []
        )
        low_consumables = (
            self._low_stock_consumables(request, target_id)
            if self.get_config_value("include_consumables", True)
            else []
        )
        low_components = (
            self._low_stock_components(request, target_id) if self.get_config_value("include_components", True) else []
        )

        return {
            "low_stock_accessories": low_accessories,
            "low_stock_consumables": low_consumables,
            "low_stock_components": low_components,
            "low_stock_accessory_count": len(low_accessories),
            "low_stock_consumable_count": len(low_consumables),
            "low_stock_component_count": len(low_components),
        }

    def _low_stock_accessories(self, request, target_id):
        acc_qs = get_scoped_queryset(Accessory, request, config=self.config.get("config", {})).filter(min_qty__gt=0)

        # Scoped total stock subquery
        stock_sub = AccessoryStock.objects.for_scope(Scope.current()).filter(accessory=OuterRef("pk"))
        if target_id:
            stock_sub = stock_sub.filter(location__tenant_id=target_id)
        stock_sub = stock_sub.values("accessory").annotate(sum_qty=Sum("qty")).values("sum_qty")

        # Scoped checked out subquery
        assignment_sub = AccessoryAssignment.objects.for_scope(Scope.current()).filter(accessory=OuterRef("pk"))
        if target_id:
            assignment_sub = assignment_sub.filter(
                Q(assigned_location__tenant_id=target_id) | Q(assigned_holder__tenant_id=target_id)
            )
        assignment_sub = assignment_sub.values("accessory").annotate(sum_qty=Sum("qty")).values("sum_qty")

        acc_qs = acc_qs.annotate(
            _total_stock_annotated=Coalesce(Subquery(stock_sub), 0),
            _checked_out_annotated=Coalesce(Subquery(assignment_sub), 0),
        )

        low_accessories = []
        for acc in acc_qs:
            available = max(0, acc._total_stock_annotated - acc._checked_out_annotated)
            if available < acc.min_qty:
                low_accessories.append(ScopedAccessoryWrapper(acc, available))
        return low_accessories

    def _low_stock_consumables(self, request, target_id):
        con_qs = get_scoped_queryset(Consumable, request, config=self.config.get("config", {})).filter(min_qty__gt=0)

        # Scoped total stock subquery
        stock_sub = ConsumableStock.objects.for_scope(Scope.current()).filter(consumable=OuterRef("pk"))
        if target_id:
            stock_sub = stock_sub.filter(location__tenant_id=target_id)
        stock_sub = stock_sub.values("consumable").annotate(sum_qty=Sum("qty")).values("sum_qty")

        # Scoped consumed subquery
        consumption_sub = ConsumableAssignment.objects.for_scope(Scope.current()).filter(consumable=OuterRef("pk"))
        if target_id:
            consumption_sub = consumption_sub.filter(
                Q(assigned_location__tenant_id=target_id) | Q(assigned_holder__tenant_id=target_id)
            )
        consumption_sub = consumption_sub.values("consumable").annotate(sum_qty=Sum("qty")).values("sum_qty")

        con_qs = con_qs.annotate(
            _total_stock_annotated=Coalesce(Subquery(stock_sub), 0),
            _consumed_annotated=Coalesce(Subquery(consumption_sub), 0),
        )

        low_consumables = []
        for con in con_qs:
            available = max(0, con._total_stock_annotated - con._consumed_annotated)
            if available < con.min_qty:
                low_consumables.append(ScopedConsumableWrapper(con, available))
        return low_consumables

    def _low_stock_components(self, request, target_id):
        comp_qs = Component.objects.for_scope(Scope.current()).filter(min_qty__gt=0).order_by("name")

        # Scoped total stock subquery
        stock_sub = ComponentStock.objects.for_scope(Scope.current()).filter(component=OuterRef("pk"))
        if target_id:
            stock_sub = stock_sub.filter(location__tenant_id=target_id)
        stock_sub = stock_sub.values("component").annotate(sum_qty=Sum("qty")).values("sum_qty")

        # Scoped allocated subquery
        allocation_sub = ComponentAllocation.objects.for_scope(Scope.current()).filter(
            component=OuterRef("pk"), deleted_at__isnull=True
        )
        if target_id:
            allocation_sub = allocation_sub.filter(assigned_asset__tenant_id=target_id)
        target_only_allocation_sub = allocation_sub.filter(from_location__isnull=True)
        allocation_sub = allocation_sub.values("component").annotate(sum_qty=Sum("qty")).values("sum_qty")
        target_only_allocation_sub = (
            target_only_allocation_sub.values("component").annotate(sum_qty=Sum("qty")).values("sum_qty")
        )

        comp_qs = comp_qs.annotate(
            _total_stock_annotated=Coalesce(Subquery(stock_sub), 0),
            _total_allocated_annotated=Coalesce(Subquery(allocation_sub), 0),
            _target_only_allocated_annotated=Coalesce(Subquery(target_only_allocation_sub), 0),
        )

        low_components = []
        for comp in comp_qs:
            available = comp._total_stock_annotated - comp._target_only_allocated_annotated
            if available < comp.min_qty:
                low_components.append(
                    {
                        "component": comp,
                        "available_stock": available,
                        "total_stock": comp._total_stock_annotated,
                        "total_allocated": comp._total_allocated_annotated,
                        "min_qty": comp.min_qty,
                    }
                )
        return low_components

    def get_footer_links(self, request):
        return [
            {"url": reverse("inventory:component_list"), "label": _("Components")},
            {"url": reverse("inventory:accessory_list"), "label": _("Accessories")},
            {"url": reverse("inventory:consumable_list"), "label": _("Consumables")},
        ]


@register_widget
class BookmarksWidget(DashboardWidget):
    widget_id = "my-bookmarks"
    icon = "star-outline"
    title = _lazy("My Bookmarks")
    description = _lazy("Quick-access list of objects you have starred (personal, per-user)")
    template_name = "extras/dashboard/widgets/bookmarks.html"

    class ConfigForm(WidgetConfigForm):
        limit = forms.IntegerField(
            label=_("Max Items to Display"),
            initial=10,
            min_value=1,
            max_value=50,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )

    def get_context(self, request):

        limit = self.get_config_value("limit") or 10
        rows = list(Bookmark.objects.filter(user=request.user).select_related("model")[:limit])
        return {"bookmarked_items": resolve_generic_items(rows)}

    def get_footer_links(self, request):

        return [{"url": reverse("users:user_bookmarks"), "label": _("All Bookmarks")}]


@register_widget
class AssetAgeWidget(DashboardWidget):
    widget_id = "asset-age"
    required_target_permissions = ("assets.view_asset",)
    icon = "chart-bar"
    title = _lazy("Asset Age Distribution")
    description = _lazy("Breakdown of assets by age bucket and average age")
    template_name = "extras/dashboard/widgets/asset_age.html"

    class ConfigForm(WidgetConfigForm):
        chart_format = forms.ChoiceField(
            label=_("Chart Format"),
            choices=[
                ("bar", _lazy("Bar Chart")),
                ("pie", _lazy("Pie Chart")),
                ("list", _lazy("List Format")),
            ],
            initial="bar",
            widget=forms.Select(attrs={"class": "form-select"}),
            required=False,
        )

    def get_context(self, request):
        today = date.today()
        assets = get_scoped_queryset(Asset, request, config=self.config.get("config", {})).filter(
            purchase_date__isnull=False
        )
        total_age = 0
        count = 0
        buckets = {"lt1y": 0, "1_3y": 0, "3_5y": 0, "5_7y": 0, "gt7y": 0}

        # Bypasses heavy Django model object creation completely
        purchase_dates = list(assets.values_list("purchase_date", flat=True))

        for p_date in purchase_dates:
            age_years = (today - p_date).days / 365.25
            total_age += age_years
            count += 1
            if age_years < 1:
                buckets["lt1y"] += 1
            elif age_years < 3:
                buckets["1_3y"] += 1
            elif age_years < 5:
                buckets["3_5y"] += 1
            elif age_years < 7:
                buckets["5_7y"] += 1
            else:
                buckets["gt7y"] += 1

        avg_age = round(total_age / count, 1) if count > 0 else 0
        chart_data = [
            {"name": "< 1 Year", "count": buckets["lt1y"], "color": "#2fb344"},
            {"name": "1 - 3 Years", "count": buckets["1_3y"], "color": "#206bc4"},
            {"name": "3 - 5 Years", "count": buckets["3_5y"], "color": "#f59f00"},
            {"name": "5 - 7 Years", "count": buckets["5_7y"], "color": "#fd7e14"},
            {"name": "7+ Years", "count": buckets["gt7y"], "color": "#d63939"},
        ]
        return {
            "age_buckets": buckets,
            "avg_asset_age_years": avg_age,
            "chart_format": self.get_config_value("chart_format", "bar"),
            "chart_data_json": json.dumps(chart_data),
        }

    def get_footer_links(self, request):
        return [{"url": reverse("assets:asset_list"), "label": _("View All Assets")}]


@register_widget
class TenantSpendWidget(DashboardWidget):
    widget_id = "tenant-spend"
    required_target_permissions = ("assets.view_asset",)
    icon = "cash-multiple"
    title = _lazy("Tenant Spend")
    description = _lazy("Purchase cost grouped by tenant (top 8)")
    template_name = "extras/dashboard/widgets/tenant_spend.html"
    # Not admin-only: a standard user sees spend grouped across exactly the
    # tenants they are authorized to access. The scope is enforced
    # by the canonical manager in get_context, never a permission gate here.

    # NOTE: the former 'chart_type' and 'currency' symbol options were dropped.
    # Assets carry a per-record currency, so each tenant bucket can mix
    # currencies; a single-symbol bar chart cannot represent (tenant x currency)
    # without summing across currencies (no FX source). Spend is now rendered as
    # a per-(tenant, currency) list formatted via the `money` filter. Stale
    # saved values for those keys are ignored.
    class ConfigForm(WidgetConfigForm):
        limit = forms.IntegerField(
            label=_("Max Tenants to Show"),
            initial=8,
            widget=forms.NumberInput(attrs={"class": "form-control"}),
            required=False,
        )
        exclude_unassigned = forms.BooleanField(
            label=_("Exclude Unassigned Assets"),
            initial=False,
            widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            required=False,
            help_text=_("Do not display spending for assets with no tenant."),
        )

    def get_context(self, request):
        limit = self.get_config_value("limit", 8)
        exclude_unassigned = self.get_config_value("exclude_unassigned", False)

        default_code = (getattr(settings, "ITAMBOX_DEFAULT_CURRENCY", "EUR") or "EUR").upper()

        # Group spend across EXACTLY the active authorized scope. The canonical
        # manager resolves it from the request/contextvars: a superuser with no
        # narrower scope gets the global cross-tenant comparison; an explicit
        # tenant / group / tenant-bound-dashboard scope, or a member's
        # "all accessible tenants" aggregate, narrows it to exactly those
        # tenants. This replaces the former Asset._base_manager global read,
        # which bypassed the scope and disclosed every tenant to any viewer.
        qs = get_scoped_queryset(Asset, request, config=self.config.get("config", {}))
        # Never surface a soft-deleted tenant in the comparison. A member's
        # accessible set already excludes them; this guard also covers the
        # superuser global view, whose unscoped rows can still reference one.
        qs = qs.filter(Q(tenant__isnull=True) | Q(tenant__deleted_at__isnull=True))
        if exclude_unassigned:
            qs = qs.filter(tenant__isnull=False)

        # Assets carry a per-record `currency`, so each tenant can mix
        # currencies. Purchase cost must NOT be summed across currencies (no FX
        # source) — group by (tenant, currency) and emit one figure per pair.
        # A single bar chart with a single hardcoded symbol cannot represent
        # (tenant x currency) without either mislabelling or summing mixed
        # currencies, so this is rendered as a per-(tenant, currency) table.
        rows = (
            qs.values("tenant__id", "tenant__name", "tenant__currency", "currency")
            .annotate(total=Sum("purchase_cost"))
            .order_by("-total")
        )

        # Aggregate per tenant; rank tenants by their largest single-currency
        # bucket (the previous ordering was by Sum(purchase_cost) desc — with
        # one currency this preserves the same top-N tenant ordering).
        tenants = {}  # tenant key -> {'name', 'currency_spend': [...], 'max_total'}
        for r in rows:
            if r["total"] is None:
                continue
            t_id = r["tenant__id"]
            name = r["tenant__name"] or _("Unassigned")
            tenant_currency = r["tenant__currency"] or ""
            code = (r["currency"] or "").upper() or (tenant_currency.upper() or default_code)
            total = float(r["total"] or 0.0)
            entry = tenants.setdefault(t_id, {"name": name, "buckets": {}, "max_total": 0.0})
            # Fold rows that resolve to the same code (e.g. blank + explicit
            # tenant-currency) into one bucket.
            entry["buckets"][code] = entry["buckets"].get(code, 0.0) + total
            entry["max_total"] = max(entry["max_total"], entry["buckets"][code])

        tenant_spend = []
        for _t_id, entry in tenants.items():
            currency_spend = []
            for code, total in sorted(entry["buckets"].items(), key=lambda kv: kv[1], reverse=True):
                currency_spend.append(
                    {
                        "currency_code": code,
                        "total": total,
                        # Explicit ISO code (never blank) for the `money` filter.
                        "currency_obj": SimpleNamespace(currency=code, tenant=None),
                    }
                )
            tenant_spend.append(
                {
                    "name": entry["name"],
                    "currency_spend": currency_spend,
                    "max_total": entry["max_total"],
                }
            )
        tenant_spend.sort(key=lambda t: t["max_total"], reverse=True)
        tenant_spend = tenant_spend[:limit]

        return {
            "tenant_spend": tenant_spend,
        }

    def get_footer_links(self, request):
        return [{"url": reverse("organization:tenant_list"), "label": _("View All Tenants")}]
