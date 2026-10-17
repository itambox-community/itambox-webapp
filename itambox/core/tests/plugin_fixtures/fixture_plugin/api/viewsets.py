from rest_framework.mixins import ListModelMixin

from assets.api.serializers import ManufacturerSerializer
from assets.models import Manufacturer
from itambox.api.viewsets import BaseViewSet


class FixtureManufacturerViewSet(ListModelMixin, BaseViewSet):
    queryset = Manufacturer.objects.all()
    serializer_class = ManufacturerSerializer


viewsets = [("manufacturers", FixtureManufacturerViewSet, "fixture_plugin-manufacturers")]
