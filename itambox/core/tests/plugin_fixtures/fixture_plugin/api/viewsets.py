from assets.api.serializers import ManufacturerSerializer
from assets.models import Manufacturer
from itambox.api.viewsets import ITAMBoxModelViewSet


class FixtureManufacturerViewSet(ITAMBoxModelViewSet):
    queryset = Manufacturer.objects.all()
    serializer_class = ManufacturerSerializer


viewsets = [("manufacturers", FixtureManufacturerViewSet, "fixture_plugin-manufacturers")]
