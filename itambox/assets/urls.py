from django.urls import include, path
from django.views.generic import RedirectView

from . import views
from .urls_audits import urlpatterns as audit_urls
from .views import request_views
from .views.disposal_views import AssetDisposalCancelView

app_name = "assets"

urlpatterns = [
    # Dashboard path removed, handled in core.urls
    # Scanner-driven bulk check-in / check-out / disposal (scan basket)
    path("assets/checkin/scan/", views.BulkCheckinScanView.as_view(), name="asset_bulk_checkin_scan"),
    path("assets/checkout/scan/", views.BulkCheckoutScanView.as_view(), name="asset_bulk_checkout_scan"),
    path("assets/dispose/scan/", views.BulkDisposeScanView.as_view(), name="asset_bulk_dispose_scan"),
    path("assets/scan/resolve-action/", views.AssetScanActionResolveView.as_view(), name="asset_scan_resolve_action"),
    path("assets/bulk-checkin/", views.bulk_checkin_assets, name="asset_bulk_checkin"),
    path("assets/bulk-checkout/", views.bulk_checkout_assets, name="asset_bulk_checkout"),
    path("assets/bulk-dispose/", views.bulk_dispose_assets, name="asset_bulk_dispose"),
    # Asset bulk operations
    path("assets/edit/", views.AssetBulkEditView.as_view(), name="asset_bulk_edit"),
    path("assets/delete/", views.AssetBulkDeleteView.as_view(), name="asset_bulk_delete"),
    # Asset specific URLs
    path("assets/", views.AssetListView.as_view(), name="asset_list"),
    path("assets/add/", views.AssetEditView.as_view(), name="asset_create"),
    path("assets/<int:pk>/", views.AssetDetailView.as_view(), name="asset_detail"),
    path("assets/<int:pk>/edit/", views.AssetEditView.as_view(), name="asset_update"),
    path("assets/<int:pk>/delete/", views.AssetDeleteView.as_view(), name="asset_delete"),
    path("assets/<int:pk>/clone/", views.AssetCloneView.as_view(), name="asset_clone"),
    path("assets/<int:pk>/checkout/", views.AssetCheckoutView.as_view(), name="asset_checkout_modal"),
    path("assets/<int:pk>/checkin/", views.AssetCheckinView.as_view(), name="asset_checkin"),
    # Asset Roles (AssetRole) URLs
    path("roles/", views.AssetRoleListView.as_view(), name="assetrole_list"),
    path("roles/add/", views.AssetRoleEditView.as_view(), name="assetrole_create"),
    path("roles/<int:pk>/", views.AssetRoleDetailView.as_view(), name="assetrole_detail"),
    path("roles/<int:pk>/edit/", views.AssetRoleEditView.as_view(), name="assetrole_update"),
    path("roles/<int:pk>/clone/", views.AssetRoleCloneView.as_view(), name="assetrole_clone"),
    path("roles/<int:pk>/delete/", views.AssetRoleDeleteView.as_view(), name="assetrole_delete"),
    # Status Labels (StatusLabel) URLs
    path("status-labels/", views.StatusLabelListView.as_view(), name="statuslabel_list"),
    path("status-labels/add/", views.StatusLabelEditView.as_view(), name="statuslabel_create"),
    path("status-labels/<int:pk>/", views.StatusLabelDetailView.as_view(), name="statuslabel_detail"),
    path("status-labels/<int:pk>/edit/", views.StatusLabelEditView.as_view(), name="statuslabel_update"),
    path("status-labels/<int:pk>/clone/", views.StatusLabelCloneView.as_view(), name="statuslabel_clone"),
    path("status-labels/<int:pk>/delete/", views.StatusLabelDeleteView.as_view(), name="statuslabel_delete"),
    # Manufacturer URLs
    path("manufacturers/", views.ManufacturerListView.as_view(), name="manufacturer_list"),
    path("manufacturers/add/", views.ManufacturerEditView.as_view(), name="manufacturer_create"),
    path("manufacturers/<int:pk>/", views.ManufacturerDetailView.as_view(), name="manufacturer_detail"),
    path("manufacturers/<int:pk>/edit/", views.ManufacturerEditView.as_view(), name="manufacturer_update"),
    path("manufacturers/<int:pk>/clone/", views.ManufacturerCloneView.as_view(), name="manufacturer_clone"),
    path("manufacturers/<int:pk>/delete/", views.ManufacturerDeleteView.as_view(), name="manufacturer_delete"),
    # Asset Types
    path("types/", views.AssetTypeListView.as_view(), name="assettype_list"),
    path("types/add/", views.AssetTypeEditView.as_view(), name="assettype_create"),
    path("types/<int:pk>/clone/", views.AssetTypeCloneView.as_view(), name="assettype_clone"),
    path("types/<int:pk>/", views.AssetTypeDetailView.as_view(), name="assettype_detail"),
    path("types/<int:pk>/edit/", views.AssetTypeEditView.as_view(), name="assettype_update"),
    path("types/<int:pk>/delete/", views.AssetTypeDeleteView.as_view(), name="assettype_delete"),
    # Components live in the inventory app/namespace (inventory:component_*).
    # The duplicate routes that used to live here are gone — one canonical URL
    # per object, the NetBox way.
    # Audits and barcoding routes.
    path("<int:pk>/audit/", views.AssetAuditView.as_view(), name="asset_audit"),
    # Depreciation
    path("depreciations/", views.DepreciationListView.as_view(), name="depreciation_list"),
    path("depreciations/add/", views.DepreciationEditView.as_view(), name="depreciation_create"),
    path("depreciations/<int:pk>/", views.DepreciationDetailView.as_view(), name="depreciation_detail"),
    path("depreciations/<int:pk>/edit/", views.DepreciationEditView.as_view(), name="depreciation_update"),
    path("depreciations/<int:pk>/clone/", views.DepreciationCloneView.as_view(), name="depreciation_clone"),
    path("depreciations/<int:pk>/delete/", views.DepreciationDeleteView.as_view(), name="depreciation_delete"),
    # Suppliers
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/add/", views.SupplierEditView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/clone/", views.SupplierCloneView.as_view(), name="supplier_clone"),
    path("suppliers/<int:pk>/", views.SupplierDetailView.as_view(), name="supplier_detail"),
    path("suppliers/<int:pk>/edit/", views.SupplierEditView.as_view(), name="supplier_update"),
    path("suppliers/<int:pk>/delete/", views.SupplierDeleteView.as_view(), name="supplier_delete"),
    # Categories
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/add/", views.CategoryEditView.as_view(), name="category_create"),
    path("categories/<int:pk>/clone/", views.CategoryCloneView.as_view(), name="category_clone"),
    path("categories/<int:pk>/", views.CategoryDetailView.as_view(), name="category_detail"),
    path("categories/<int:pk>/edit/", views.CategoryEditView.as_view(), name="category_update"),
    path("categories/<int:pk>/delete/", views.CategoryDeleteView.as_view(), name="category_delete"),
    # Asset Requests
    path("requests/", views.AssetRequestListView.as_view(), name="assetrequest_list"),
    path("requests/add/", views.AssetRequestCreateView.as_view(), name="assetrequest_create"),
    path("requests/<int:pk>/", request_views.RequestDetailView.as_view(), name="assetrequest_detail"),
    path("requests/<int:pk>/edit/", views.AssetRequestEditView.as_view(), name="assetrequest_update"),
    path("requests/<int:pk>/delete/", views.AssetRequestDeleteView.as_view(), name="assetrequest_delete"),
    path("asset-tag-sequences/", views.AssetTagSequenceListView.as_view(), name="assettagsequence_list"),
    path("asset-tag-sequences/add/", views.AssetTagSequenceEditView.as_view(), name="assettagsequence_create"),
    path("asset-tag-sequences/<int:pk>/", views.AssetTagSequenceDetailView.as_view(), name="assettagsequence_detail"),
    path(
        "asset-tag-sequences/<int:pk>/edit/", views.AssetTagSequenceEditView.as_view(), name="assettagsequence_update"
    ),
    path(
        "asset-tag-sequences/<int:pk>/delete/",
        views.AssetTagSequenceDeleteView.as_view(),
        name="assettagsequence_delete",
    ),
    # Custom fields moved to the extras app (extras:customfield_*).
    # Redirects keep old bookmarks alive.
    path("custom-fields/", RedirectView.as_view(pattern_name="extras:customfield_list", permanent=True)),
    path("custom-fields/<int:pk>/", RedirectView.as_view(pattern_name="extras:customfield_detail", permanent=True)),
    path("custom-fieldsets/", RedirectView.as_view(pattern_name="extras:customfieldset_list", permanent=True)),
    path(
        "custom-fieldsets/<int:pk>/", RedirectView.as_view(pattern_name="extras:customfieldset_detail", permanent=True)
    ),
    # Asset Request routes
    path("", include("assets.urls_requests")),
]


urlpatterns += audit_urls

urlpatterns += [
    path("maintenances/", views.AssetMaintenanceListView.as_view(), name="assetmaintenance_list"),
    path("maintenances/add/", views.AssetMaintenanceEditView.as_view(), name="assetmaintenance_create"),
    path("maintenances/<int:pk>/", views.AssetMaintenanceDetailView.as_view(), name="assetmaintenance_detail"),
    path("maintenances/<int:pk>/edit/", views.AssetMaintenanceEditView.as_view(), name="assetmaintenance_update"),
    path("maintenances/<int:pk>/clone/", views.AssetMaintenanceCloneView.as_view(), name="assetmaintenance_clone"),
    path("maintenances/<int:pk>/delete/", views.AssetMaintenanceDeleteView.as_view(), name="assetmaintenance_delete"),
]

urlpatterns += [
    # Disposal list + CRUD
    path("disposals/", views.AssetDisposalListView.as_view(), name="assetdisposal_list"),
    path("disposals/add/", views.AssetDisposalEditView.as_view(), name="assetdisposal_create"),
    path("disposals/<int:pk>/", views.AssetDisposalDetailView.as_view(), name="assetdisposal_detail"),
    path("disposals/<int:pk>/edit/", views.AssetDisposalEditView.as_view(), name="assetdisposal_update"),
    path("disposals/<int:pk>/delete/", views.AssetDisposalDeleteView.as_view(), name="assetdisposal_delete"),
    # Cancel an erroneous disposal while preserving the record as evidence
    path("disposals/<int:pk>/cancel/", AssetDisposalCancelView.as_view(), name="assetdisposal_cancel"),
    # Asset-specific dispose action (pre-fills asset, calls dispose_asset service)
    path("assets/<int:pk>/dispose/", views.AssetDisposeActionView.as_view(), name="asset_dispose"),
]

urlpatterns += [
    # Warranty CRUD
    path("warranties/", views.WarrantyListView.as_view(), name="warranty_list"),
    path("warranties/add/", views.WarrantyEditView.as_view(), name="warranty_create"),
    path("warranties/<int:pk>/", views.WarrantyDetailView.as_view(), name="warranty_detail"),
    path("warranties/<int:pk>/edit/", views.WarrantyEditView.as_view(), name="warranty_update"),
    path("warranties/<int:pk>/delete/", views.WarrantyDeleteView.as_view(), name="warranty_delete"),
]

urlpatterns += [
    # Asset Reservation CRUD
    path("reservations/", views.AssetReservationListView.as_view(), name="assetreservation_list"),
    path("reservations/add/", views.AssetReservationEditView.as_view(), name="assetreservation_create"),
    path("reservations/<int:pk>/", views.AssetReservationDetailView.as_view(), name="assetreservation_detail"),
    path("reservations/<int:pk>/edit/", views.AssetReservationEditView.as_view(), name="assetreservation_update"),
    path("reservations/<int:pk>/delete/", views.AssetReservationDeleteView.as_view(), name="assetreservation_delete"),
]
