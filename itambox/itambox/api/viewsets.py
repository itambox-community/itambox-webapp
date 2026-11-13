import logging
from functools import cached_property

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, PermissionDenied
from django.db import router, transaction
from django.db.models import ProtectedError, RestrictedError
from rest_framework import mixins as drf_mixins
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from core.archive_handlers import archive_object
from core.context import get_current_all_accessible, get_current_tenant_group
from core.managers import Scope, TenantScopeDeclaration, get_current_tenant, tenant_scope_declaration
from core.tenant_scope import get_descendant_tenant_group_ids
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
        # tenant context is active, so for_scope() is a no-op then.  Re-apply it here at
        # request time so the correct tenant scope is enforced on every call. A collection
        # list additionally narrows to the tenants where the caller holds the model's view
        # permission: a tenant-group/all-accessible scope can reach a tenant the caller is
        # not authorized to read there, and the ambient permission gate passes on the union
        # across the scope, not this per-tenant intersection (issue #1088). Detail, update
        # and delete are unaffected -- they keep the existing object-level check.
        if hasattr(qs, "for_scope"):
            # Narrow by the permission that governs this request, not only for the
            # plain list action: a collection read issued through a custom
            # ``detail=False`` action, and the bulk update/destroy routes (which
            # filter ``get_queryset()`` by pk and never run a per-object
            # ``has_object_permission``), would otherwise still operate on rows from
            # a reachable-but-unauthorized tenant (#1088).
            scope_action = None
            action = getattr(self, "action", None)
            method = getattr(getattr(self, "request", None), "method", None)
            if action == "list" or (action is not None and method in SAFE_METHODS):
                scope_action = "view"
            elif action in ("bulk_update", "bulk_partial_update"):
                scope_action = "change"
            elif action == "bulk_destroy":
                scope_action = "delete"
            required_permission = (
                f"{qs.model._meta.app_label}.{scope_action}_{qs.model._meta.model_name}" if scope_action else None
            )
            # Bulk update/destroy never run a per-object permission check, so a
            # group-shared row may only enter their queryset when the caller is
            # authorized in every tenant that row affects (writes affect the
            # whole group subtree; reads only need one visible tenant).
            group_scope = "all" if action in ("bulk_update", "bulk_partial_update", "bulk_destroy") else "visible"
            qs = qs.for_scope(Scope.current(), required_permission=required_permission, group_scope=group_scope)

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

    @staticmethod
    def _resolve_lookup_target_tenant(lookup, row):
        """Walk a ``tenant_lookup`` path (e.g. ``"asset__tenant"``) through
        ``row`` (a ``validated_data`` dict) to the parent's tenant instance.

        The first segment names a related field on the row itself (already
        resolved to a model instance by the serializer's relational field);
        the remaining segments are plain attribute access on that instance.
        Any missing link (an optional relation left blank, or a parent that is
        itself global) resolves to ``None`` rather than raising -- the caller
        then treats the row as having no tenant-scoped target to authorize.
        """
        parts = lookup.split("__")
        value = row.get(parts[0]) if isinstance(row, dict) else None
        for part in parts[1:]:
            if value is None:
                return None
            value = getattr(value, part, None)
        return value

    @staticmethod
    def _group_target_tenants(group):
        """Every live tenant in ``group``'s own subtree.

        # unscoped: authorization must see the group's full membership regardless
        # of the caller's own ambient tenant scope.
        """
        Tenant = apps.get_model("organization", "Tenant")
        descendant_group_ids = get_descendant_tenant_group_ids(group.pk, live_only=True)
        return list(Tenant._base_manager.filter(group_id__in=descendant_group_ids, deleted_at__isnull=True))

    def _effective_create_target(self, row, declaration, save_kwargs, bulk):
        """The Tenant or TenantGroup a create ``row`` will be visible from."""
        tenant = row.get("tenant") if isinstance(row, dict) else None
        tenant_group = row.get("tenant_group") if isinstance(row, dict) else None
        if tenant is None and not bulk:
            # A non-bulk row with no explicit tenant was pinned to the active
            # tenant via `save_kwargs` (`_tenant_create_kwargs`), not mutated
            # into `validated_data` the way bulk rows are.
            tenant = save_kwargs.get("tenant")
        if tenant is not None:
            return tenant
        if tenant_group is not None:
            return tenant_group
        if declaration.strategy == TenantScopeDeclaration.LOOKUP:
            return self._resolve_lookup_target_tenant(declaration.lookup, row)
        return None

    def _user_authorized_for_target(self, user, perm, target):
        """Authorize ``perm`` against a resolved create target.

        A ``Tenant`` target is the ordinary case: ``has_perm`` resolves it
        directly. A ``TenantGroup`` target (an explicit group-scoped
        definition row, e.g. a group-shared Supplier) fans out to every live
        tenant in that group -- `_scope_by_group_field` makes the saved row
        visible from each of them -- so the caller must hold the add
        permission in every one of those tenants, not merely one. This is the
        deliberately strict reading: a single per-tenant grant must not be
        enough to write a row the whole group will see (#1089). An empty
        group authorizes nothing, since there is no tenant to anchor the
        permission against.
        """
        TenantGroup = apps.get_model("organization", "TenantGroup")
        if isinstance(target, TenantGroup):
            tenants = self._group_target_tenants(target)
            return bool(tenants) and all(user.has_perm(perm, obj=tenant) for tenant in tenants)
        return user.has_perm(perm, obj=target)

    def _authorize_create(self, serializer, model, save_kwargs):
        """Authorize every create row against its effective target tenant
        before anything is written (#1089).

        The ambient create gate (`TokenPermissions.has_permission`) passes an
        object-less `add_<model>` check that, under a tenant-group or
        all-accessible scope, is satisfied by holding the permission in ANY
        tenant in scope. That gate never re-checks the permission against the
        row's actual target once the client supplies an explicit `tenant` (or
        `tenant_group`, or a parent-derived tenant). This closes that gap by
        resolving each row's effective target and re-checking the add
        permission against it directly, before `serializer.save()`.

        Bulk create is all-or-nothing: the whole request is rejected (not
        just the offending rows) so a client cannot discover which rows would
        have succeeded, and nothing is written for a request that is
        partially unauthorized.
        """
        user = self.request.user
        if user.is_superuser:
            return
        bulk = getattr(serializer, "many", False)
        validated = serializer.validated_data
        rows = validated if bulk else [validated]
        declaration = tenant_scope_declaration(model)
        perm = f"{model._meta.app_label}.add_{model._meta.model_name}"
        for row in rows:
            target = self._effective_create_target(row, declaration, save_kwargs, bulk)
            if target is None:
                # Fail closed when a tenant-scoped model's target cannot be
                # resolved under an aggregate scope. Skipping would write a row
                # whose visibility no per-tenant check ever covered; a model with
                # no tenant concept (declaration NONE) legitimately has no target.
                if declaration.strategy != TenantScopeDeclaration.NONE and (
                    get_current_all_accessible() or get_current_tenant_group() is not None
                ):
                    raise PermissionDenied()
                continue
            if not self._user_authorized_for_target(user, perm, target):
                raise PermissionDenied()

    def perform_create(self, serializer):
        model = self._get_model(serializer=serializer)
        logger.info(f"Creating new {model._meta.verbose_name}")

        save_kwargs = self._tenant_create_kwargs(serializer, model)
        self._authorize_create(serializer, model, save_kwargs)

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
