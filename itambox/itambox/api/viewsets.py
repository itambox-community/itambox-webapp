import logging
from functools import cached_property

from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, PermissionDenied
from django.db import router, transaction
from django.db.models import ProtectedError, RestrictedError
from rest_framework import mixins as drf_mixins
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from core.archive_handlers import archive_object
from core.managers import Scope, get_current_tenant
from itambox.api.mixins import BulkDestroyModelMixin, BulkUpdateModelMixin, ETagMixin, ObjectValidationMixin
from itambox.api.serializers.features import ChangeLogMessageSerializer
from itambox.api.utils import get_annotations_for_serializer, get_prefetches_for_serializer

logger = logging.getLogger("itambox.api.views")


def _scoped_manager(model):
    """Default manager narrowed to the ambient scope when the model supports it."""
    manager = model.objects
    return manager.for_scope(Scope.current()) if hasattr(manager, "for_scope") else manager.all()


class BaseViewSet(GenericViewSet):
    brief = False

    def initialize_request(self, request, *args, **kwargs):
        self.brief = request.method == "GET" and "brief" in request.GET
        return super().initialize_request(request, *args, **kwargs)

    def get_object(self):
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        if lookup_url_kwarg not in self.kwargs:
            # SimpleMetadata probes PUT while handling OPTIONS on collection routes.
            # Convert GenericAPIView's missing-lookup assertion into an exception
            # that the metadata probe treats as an unavailable detail action.
            raise NotFound()
        return super().get_object()

    def get_queryset(self):
        qs = super().get_queryset()

        # The viewset's `queryset` class attribute is evaluated at import time when no
        # tenant context is active, so filter_by_tenant() is a no-op then.  Re-apply it
        # here at request time so the correct tenant scope is enforced on every call.
        if hasattr(qs, "filter_by_tenant"):
            qs = qs.filter_by_tenant()

        serializer_class = self.get_serializer_class()

        if not hasattr(serializer_class, "Meta") or not hasattr(serializer_class.Meta, "model"):
            return qs

        if prefetch := get_prefetches_for_serializer(serializer_class, **self.field_kwargs):
            qs = qs.prefetch_related(*prefetch)

        if annotations := get_annotations_for_serializer(serializer_class, **self.field_kwargs):
            qs = qs.annotate(**annotations)

        return qs

    def get_serializer(self, *args, **kwargs):
        kwargs.update(**self.field_kwargs)
        return super().get_serializer(*args, **kwargs)

    @cached_property
    def field_kwargs(self):
        if requested_fields := self.request.query_params.get("fields"):
            return {"fields": requested_fields.split(",")}

        if omit_fields := self.request.query_params.get("omit"):
            return {"omit": omit_fields.split(",")}

        if self.brief:
            serializer_class = self.get_serializer_class()
            if brief_fields := getattr(serializer_class.Meta, "brief_fields", None):
                return {"fields": brief_fields}

        return {}


class ITAMBoxReadOnlyModelViewSet(ETagMixin, drf_mixins.RetrieveModelMixin, drf_mixins.ListModelMixin, BaseViewSet):
    pass


class ITAMBoxModelViewSet(
    ETagMixin,
    BulkUpdateModelMixin,
    BulkDestroyModelMixin,
    ObjectValidationMixin,
    drf_mixins.CreateModelMixin,
    drf_mixins.RetrieveModelMixin,
    drf_mixins.UpdateModelMixin,
    drf_mixins.DestroyModelMixin,
    drf_mixins.ListModelMixin,
    BaseViewSet,
):
    @staticmethod
    def _ensure_unmanaged_definition(instance):
        if getattr(instance, "management_kind", "local") != "local":
            raise PermissionDenied("Managed definitions cannot be mutated through the API.")

    def get_object_with_snapshot(self):
        obj = super().get_object()
        if hasattr(obj, "snapshot"):
            obj.snapshot()
        return obj

    def get_serializer(self, *args, **kwargs):
        if isinstance(kwargs.get("data", {}), list):
            kwargs["many"] = True
        return super().get_serializer(*args, **kwargs)

    def _get_model(self, *, serializer=None, instance=None):
        if self.queryset is not None:
            return self.queryset.model
        if serializer is not None:
            return getattr(serializer, "child", serializer).Meta.model
        if instance is not None:
            return type(instance)
        raise AssertionError("A queryset, serializer, or instance is required to derive the viewset model.")

    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except (ProtectedError, RestrictedError) as e:
            if type(e) is ProtectedError:
                protected_objects = e.protected_objects
            else:
                protected_objects = e.restricted_objects
            # Report only a COUNT of blocking dependents. Enumerating each as
            # '{obj} ({obj.pk})' leaked the str()/pk of related rows the caller
            # may not be entitled to see (e.g. cross-tenant or hidden objects).
            count = len(protected_objects)
            # Keep the enumerated detail server-side for diagnostics only.
            logger.warning(
                "Unable to delete object: %d dependent object(s): %s",
                count,
                ", ".join(f"{obj} ({obj.pk})" for obj in protected_objects),
            )
            msg = f"Unable to delete object. {count} dependent object(s) reference it."
            return self.finalize_response(request, Response({"detail": msg}, status=409), *args, **kwargs)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bulk_create = getattr(serializer, "many", False)
        self.perform_create(serializer)

        qs = self.get_created_response_instance(serializer)
        serializer = self.get_serializer(qs, many=bulk_create)

        headers = self.get_success_headers(serializer.data)
        response = Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

        if not bulk_create:
            if etag := self._get_etag(qs):
                response["ETag"] = etag

        return response

    def get_created_response_instance(self, serializer):
        bulk_create = getattr(serializer, "many", False)
        if bulk_create:
            instance_pks = [obj.pk for obj in serializer.instance]
            return self.get_queryset().filter(pk__in=instance_pks).order_by("pk")
        try:
            return self.get_queryset().get(pk=serializer.instance.pk)
        except ObjectDoesNotExist:
            # The object was just created and validated by this request, so
            # re-fetching it is safe even when the scoped queryset (e.g. one
            # filtered through asset__tenant) returns .none() because no
            # tenant is bound in the current context (e.g. tests, service
            # accounts).  Fall back to the unsaved instance rather than
            # raising an unexpected 500.
            return serializer.instance

    @staticmethod
    def _missing_create_tenant_rows(serializer):
        # A row that explicitly carries a tenant group is already scoped:
        # injecting the ambient tenant on top of it would violate the tenant
        # XOR tenant-group constraint, so only rows that leave BOTH scope
        # fields empty count as missing a tenant.
        validated = serializer.validated_data
        if getattr(serializer, "many", False):
            return [
                row
                for row in validated
                if isinstance(row, dict) and row.get("tenant") is None and row.get("tenant_group") is None
            ]
        if isinstance(validated, dict) and validated.get("tenant") is None and validated.get("tenant_group") is None:
            return [validated]
        return []

    def _tenant_create_kwargs(self, serializer, model):
        # Default a tenant-scoped create to the active tenant when the client
        # omitted it OR explicitly passed a null tenant.  Without this a
        # tenant-bound (non-superuser) request could mint a global (tenant=None)
        # row — e.g. a globally-visible License or Software — cross-tenant,
        # either by omitting the field or by sending an explicit
        # {"tenant": null} that lands as `tenant: None` in validated_data and
        # would otherwise slip past an `'tenant' not in validated` check.
        # Superusers retain the ability to create global rows explicitly
        # (including via an explicit null). For a bulk ListSerializer, fill only
        # rows that omitted/nullified tenant; preserve explicit tenants so the
        # request-scoped object validation below can accept or reject each one.
        if self.request.user.is_superuser:
            return {}

        try:
            tenant_field = model._meta.get_field("tenant")
        except FieldDoesNotExist:
            return {}
        if not getattr(tenant_field, "null", False):
            return {}

        bulk_create = getattr(serializer, "many", False)
        missing_tenant_rows = self._missing_create_tenant_rows(serializer)
        if not missing_tenant_rows:
            return {}

        active_tenant = get_current_tenant()
        if active_tenant is None:
            # No single active tenant (tenant-group / All-accessible / unbound
            # context): deriving one from a membership/AssetHolder would cross
            # the authorization boundary — fail closed.
            raise PermissionDenied()

        if not bulk_create:
            return {"tenant": active_tenant}

        # ListSerializer.save() consumes these per-row mappings; mutate only
        # missing/null values so explicit tenant choices remain subject to
        # _validate_objects().
        for row in missing_tenant_rows:
            row["tenant"] = active_tenant
        return {}

    def perform_create(self, serializer):
        model = self._get_model(serializer=serializer)
        logger.info(f"Creating new {model._meta.verbose_name}")

        save_kwargs = self._tenant_create_kwargs(serializer, model)

        try:
            with transaction.atomic(using=router.db_for_write(model)):
                instance = serializer.save(**save_kwargs)
                self._validate_objects(instance)
        except ObjectDoesNotExist:
            raise PermissionDenied() from None

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object_with_snapshot()

        self._validate_etag(self.request, instance)

        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        try:
            qs = self.get_queryset().get(pk=serializer.instance.pk)
        except ObjectDoesNotExist:
            # The update may legitimately move the object out of the
            # request-scoped queryset (e.g. a superuser transferring a token
            # to another user. The instance was validated and
            # committed by perform_update() above; serve it directly rather
            # than failing with a 500 after the commit. Mirrors
            # get_created_response_instance().
            qs = serializer.instance
        serializer = self.get_serializer(qs)
        response = Response(serializer.data)

        if etag := self._get_etag(qs):
            response["ETag"] = etag

        return response

    def perform_update(self, serializer):
        self._ensure_unmanaged_definition(serializer.instance)
        model = self._get_model(serializer=serializer)
        logger.info(f"Updating {model._meta.verbose_name} {serializer.instance} (PK: {serializer.instance.pk})")

        save_kwargs = {}
        # Re-pin the object scope on update for non-superusers.  A PATCH/PUT
        # that sets tenant=null or tenant_group=null (or a different scope) on
        # an allow_global_tenant model would otherwise globalize the row —
        # making it visible to every tenant — or move it into another tenant
        # entirely, a cross-tenant read.  The tenant_group path needs the same
        # protection: {"tenant_group_id": null} would turn a group-scoped row
        # into a global one, and the tenant pin alone would not catch it.
        # Force both scope fields back to the object's existing values.
        # Superusers may still retarget scope explicitly.  Mirrors
        # perform_create's tenant pinning and the GraphQL mutations, which
        # never let a client set tenant.
        validated = serializer.validated_data
        if (
            not getattr(serializer, "many", False)
            and isinstance(validated, dict)
            and not self.request.user.is_superuser
        ):
            for scope_field in ("tenant", "tenant_group"):
                if scope_field not in validated:
                    continue
                try:
                    model._meta.get_field(scope_field)
                except FieldDoesNotExist:
                    continue
                save_kwargs[scope_field] = getattr(serializer.instance, scope_field, None)

        try:
            with transaction.atomic(using=router.db_for_write(model)):
                locked = _scoped_manager(model).select_for_update().get(pk=serializer.instance.pk)
                self._ensure_unmanaged_definition(locked)
                self._validate_etag(self.request, locked)
                instance = serializer.save(**save_kwargs)
                self._validate_objects(instance)
        except ObjectDoesNotExist:
            raise PermissionDenied() from None

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object_with_snapshot()

        self._validate_etag(request, instance)

        serializer = ChangeLogMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance._changelog_message = serializer.validated_data.get("changelog_message")

        self.perform_destroy(instance)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def perform_destroy(self, instance):
        self._ensure_unmanaged_definition(instance)
        model = self._get_model(instance=instance)
        logger.info(f"Deleting {model._meta.verbose_name} {instance} (PK: {instance.pk})")

        try:
            with transaction.atomic(using=router.db_for_write(model)):
                locked = _scoped_manager(model).select_for_update().get(pk=instance.pk)
                self._ensure_unmanaged_definition(locked)
                self._validate_etag(self.request, locked)
                archive_object(locked, actor=self.request.user, request=self.request)
        except ObjectDoesNotExist:
            logger.warning(
                "perform_destroy: %s pk=%s not visible in tenant scope; denying.",
                model._meta.verbose_name,
                instance.pk,
            )
            raise PermissionDenied() from None
