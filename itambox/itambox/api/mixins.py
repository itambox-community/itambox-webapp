import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import router, transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.response import Response

from itambox.api.exceptions import PreconditionFailed, PreconditionRequired
from itambox.api.serializers.bulk import BulkOperationSerializer

logger = logging.getLogger("itambox.api.views")


class ETagMixin:
    @staticmethod
    def _get_etag(obj):
        if ts := getattr(obj, "last_updated", None) or getattr(obj, "updated_at", None):
            return 'W/"%s"' % ts.isoformat()
        return None

    @staticmethod
    def _get_if_match(request):
        if (if_match := request.META.get("HTTP_IF_MATCH")) and if_match != "*":
            return [e.strip() for e in if_match.split(",")]
        return []

    def _require_etag(self, request, instance):
        if not self._get_if_match(request):
            raise PreconditionRequired(
                detail=_("If-Match header is required for mutating requests."),
                etag=self._get_etag(instance),
            )

    def _validate_etag(self, request, instance):
        self._require_etag(request, instance)
        if provided := self._get_if_match(request):
            current_etag = self._get_etag(instance)
            if current_etag and current_etag not in provided:
                raise PreconditionFailed(etag=current_etag)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        response = Response(serializer.data)

        if etag := self._get_etag(instance):
            response["ETag"] = etag

        return response


class BulkUpdateModelMixin:
    def get_bulk_update_queryset(self):
        return self.get_queryset()

    def bulk_update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        serializer = BulkOperationSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        qs = self.get_bulk_update_queryset().filter(pk__in=[o["id"] for o in serializer.validated_data])

        update_data = {obj["id"]: {k: v for k, v in obj.items() if k != "id"} for obj in request.data}

        object_pks = self.perform_bulk_update(qs, update_data, partial=partial)
        qs = self.get_queryset().filter(pk__in=object_pks)
        serializer = self.get_serializer(qs, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

    def perform_bulk_update(self, objects, update_data, partial):
        updated_pks = []
        with transaction.atomic(using=router.db_for_write(objects.model)):
            for obj in objects:
                data = update_data.get(obj.id)
                if hasattr(obj, "snapshot"):
                    obj.snapshot()
                serializer = self.get_serializer(obj, data=data, partial=partial)
                serializer.is_valid(raise_exception=True)
                self.perform_update(serializer)
                updated_pks.append(obj.pk)

        return updated_pks

    def bulk_partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.bulk_update(request, *args, **kwargs)


class BulkDestroyModelMixin:
    def get_bulk_destroy_queryset(self):
        return self.get_queryset()

    def bulk_destroy(self, request, *args, **kwargs):
        serializer = BulkOperationSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        qs = self.get_bulk_destroy_queryset().filter(pk__in=[o["id"] for o in serializer.validated_data])

        changelog_messages = {o["id"]: o.get("changelog_message") for o in serializer.validated_data}

        self.perform_bulk_destroy(qs, changelog_messages)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_bulk_destroy(self, objects, changelog_messages=None):
        changelog_messages = changelog_messages or {}
        with transaction.atomic(using=router.db_for_write(objects.model)):
            for obj in objects:
                if hasattr(obj, "snapshot"):
                    obj.snapshot()
                obj._changelog_message = changelog_messages.get(obj.pk)
                self.perform_destroy(obj)


class ObjectValidationMixin:
    def _validate_objects(self, instance):
        # Validate against the request-time, tenant-scoped queryset rather than
        # the import-time `self.queryset` class attribute (which is evaluated
        # with no tenant bound and is therefore unscoped).
        qs = self.get_queryset()
        if type(instance) is list:
            conforming_count = qs.filter(pk__in=[obj.pk for obj in instance]).count()
            if conforming_count != len(instance):
                raise ObjectDoesNotExist
        elif not qs.filter(pk=instance.pk).exists():
            raise ObjectDoesNotExist
