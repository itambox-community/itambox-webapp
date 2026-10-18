from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from itambox.plugins import diagnostics


class PluginTemplateExtensionSerializer(serializers.Serializer):
    model = serializers.CharField()
    class_name = serializers.CharField()


class PluginViewSetSerializer(serializers.Serializer):
    prefix = serializers.CharField()
    viewset = serializers.CharField()
    basename = serializers.CharField(allow_null=True)


class PluginResourcesSerializer(serializers.Serializer):
    template_extensions = PluginTemplateExtensionSerializer(many=True)
    menu = serializers.ListField(child=serializers.CharField())
    menu_items = serializers.ListField(child=serializers.CharField())
    viewsets = PluginViewSetSerializer(many=True)
    permission_targets = serializers.ListField(child=serializers.CharField())
    graphql_schema = serializers.ListField(child=serializers.CharField())


class PluginInventorySerializer(serializers.Serializer):
    plugin = serializers.CharField()
    version = serializers.CharField(allow_blank=True)
    itambox_range = serializers.CharField(allow_blank=True, allow_null=True)
    state = serializers.ChoiceField(choices=("active", "skipped"))
    running_version = serializers.CharField(allow_blank=True)
    resources = PluginResourcesSerializer()
    applied_migrations = serializers.ListField(child=serializers.CharField(), allow_null=True)
    pending_migrations = serializers.ListField(child=serializers.CharField(), allow_null=True)


class PluginInventoryResponseSerializer(serializers.Serializer):
    plugins = PluginInventorySerializer(many=True)


class ViewPluginDiagnosticsPermission(BasePermission):
    message = "You do not have permission to view plugin diagnostics."

    def has_permission(self, request, view):
        return request.user.has_perm("core.view_plugin_diagnostics")


class PluginDiagnosticsView(APIView):
    """Read-only operator inventory for configured plugins."""

    permission_classes = (IsAuthenticated, ViewPluginDiagnosticsPermission)

    @extend_schema(
        operation_id="plugin_diagnostics_list",
        summary="List configured plugins and their diagnostics",
        description="Requires an authenticated user with core.view_plugin_diagnostics.",
        responses=PluginInventoryResponseSerializer,
        tags=["Plugin Diagnostics"],
    )
    def get(self, request):
        serializer = PluginInventoryResponseSerializer(instance={"plugins": diagnostics.plugin_inventory()})
        return Response(serializer.data)
