from extras.models import Dashboard


def get_default_dashboard():
    """Return the default layout config for a fresh dashboard.

    Layout transcribed from the hand-tuned "COPYTHISLAYOUT" reference
    dashboard (2026-06-12). Explicit x/y positions are included so GridStack
    reproduces the arrangement exactly instead of auto-packing.
    """
    return [
        # Row 1 (y=0, h3) — personal + alerts strip: bookmarks, stock, renewals, notes
        {
            "widget": "my-bookmarks",
            "title": "My Bookmarks",
            "visible": True,
            "x": 0,
            "y": 0,
            "w": 3,
            "h": 3,
            "style": "default",
            "config": {},
        },
        {
            "widget": "low-stock",
            "title": "Low Stock Alerts",
            "visible": True,
            "x": 3,
            "y": 0,
            "w": 3,
            "h": 3,
            "style": "danger",
            "config": {},
        },
        {
            "widget": "upcoming-renewals",
            "title": "Upcoming Renewals",
            "visible": True,
            "x": 6,
            "y": 0,
            "w": 3,
            "h": 3,
            "style": "warning",
            "config": {},
        },
        {
            "widget": "note",
            "title": "Quick Notes",
            "visible": True,
            "x": 9,
            "y": 0,
            "w": 3,
            "h": 3,
            "style": "default",
            "config": {
                "content": (
                    "# Welcome to ITAMbox!\n\n"
                    "This is your dashboard. Click the gear icon on any widget to adjust "
                    "parameters, or click Unlock at the top right to drag, resize, or reorder elements.\n\n"
                    "- Star any object to see it under My Bookmarks\n"
                    "- Press Ctrl+K to jump to search from anywhere"
                )
            },
        },
        # Row 2 (y=3, h4) — status donut, financial centerpiece, tenant spend, counts
        {
            "widget": "status-labels",
            "title": "Asset Status Labels",
            "visible": True,
            "x": 0,
            "y": 3,
            "w": 3,
            "h": 4,
            "style": "info",
            "config": {"chart_type": "doughnut"},
        },
        {
            "widget": "financial-overview",
            "title": "Financial Overview",
            "visible": True,
            "x": 3,
            "y": 3,
            "w": 4,
            "h": 4,
            "style": "success",
            "config": {
                "metrics": ["purchase", "maintenance", "salvage", "asset_count"],
            },
        },
        {
            "widget": "tenant-spend",
            "title": "Tenant Spend",
            "visible": True,
            "x": 7,
            "y": 3,
            "w": 3,
            "h": 4,
            "style": "info",
            "config": {"limit": 6},
        },
        {
            "widget": "object-counts",
            "title": "Object Counts",
            "visible": True,
            "x": 10,
            "y": 3,
            "w": 2,
            "h": 4,
            "style": "info",
            "config": {
                "models": [
                    "assets.asset",
                    "inventory.component",
                    "inventory.accessory",
                    "inventory.consumable",
                    "organization.tenant",
                    "organization.location",
                    "licenses.license",
                    "subscriptions.subscription",
                    "software.software",
                ],
            },
        },
        # Row 3 (y=7, h4) — age chart, EOL planning, repairs, license seats
        {
            "widget": "asset-age",
            "title": "Asset Age Distribution",
            "visible": True,
            "x": 0,
            "y": 7,
            "w": 3,
            "h": 4,
            "style": "info",
            "config": {"chart_format": "bar"},
        },
        {
            "widget": "eol-alerts",
            "title": "EOL Planning Alerts",
            "visible": True,
            "x": 3,
            "y": 7,
            "w": 4,
            "h": 4,
            "style": "warning",
            "config": {},
        },
        {
            "widget": "active-maintenances",
            "title": "Active Repairs & Maintenances",
            "visible": True,
            "x": 7,
            "y": 7,
            "w": 3,
            "h": 4,
            "style": "warning",
            "config": {},
        },
        {
            "widget": "license-utilization",
            "title": "Software License Seats",
            "visible": True,
            "x": 10,
            "y": 7,
            "w": 2,
            "h": 4,
            "style": "info",
            "config": {},
        },
        # Row 4 (y=11, h5) — full-width audit trail
        {
            "widget": "recent-activity",
            "title": "Change Log",
            "visible": True,
            "x": 0,
            "y": 11,
            "w": 12,
            "h": 5,
            "style": "default",
            "config": {"limit": 8},
        },
    ]


def get_dashboard(user, dashboard_id=None, for_update=False):
    """Get or create a dashboard for the given user.

    Args:
        user: The user whose dashboard to fetch.
        dashboard_id: Optional ID of the specific dashboard to fetch.
        for_update: If True, locks the row with SELECT ... FOR UPDATE
                    to prevent concurrent mutation race conditions.
    """
    qs = Dashboard.objects.filter(user=user)
    if for_update:
        qs = qs.select_for_update()

    dashboard = None
    if dashboard_id:
        dashboard = qs.filter(id=dashboard_id).first()

    if not dashboard:
        dashboard = qs.filter(is_default=True).first()

    if not dashboard:
        dashboard = qs.first()

    if not dashboard:
        dashboard = Dashboard.objects.create(
            user=user, name="Main Dashboard", is_default=True, layout=get_default_dashboard()
        )
    elif not dashboard.layout:
        dashboard.layout = get_default_dashboard()
        dashboard.save(update_fields=["layout"])

    return dashboard
