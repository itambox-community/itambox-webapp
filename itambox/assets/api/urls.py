from itambox.api.routers import ITAMBoxRouter

from .views import (
    AssetAssignmentViewSet,
    AssetDisposalViewSet,
    AssetRequestViewSet,
    AssetReservationViewSet,
    AssetRoleViewSet,
    AssetTagSequenceViewSet,
    AssetTypeViewSet,
    AssetViewSet,
    CategoryViewSet,
    DepreciationViewSet,
    ManufacturerViewSet,
    StatusLabelViewSet,
    SupplierViewSet,
    WarrantyViewSet,
)

app_name = "assets_api"

router = ITAMBoxRouter()
router.register(r"assets", AssetViewSet)
router.register(r"asset-roles", AssetRoleViewSet)
router.register(r"manufacturers", ManufacturerViewSet)
router.register(r"asset-types", AssetTypeViewSet)
router.register(r"status-labels", StatusLabelViewSet)
router.register(r"depreciations", DepreciationViewSet)
router.register(r"suppliers", SupplierViewSet)
router.register(r"categories", CategoryViewSet)
router.register(r"asset-requests", AssetRequestViewSet)
router.register(r"asset-tag-sequences", AssetTagSequenceViewSet)
router.register(r"asset-assignments", AssetAssignmentViewSet)
router.register(r"asset-disposals", AssetDisposalViewSet)
router.register(r"warranties", WarrantyViewSet)
router.register(r"asset-reservations", AssetReservationViewSet)

urlpatterns = router.urls
