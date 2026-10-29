from collections.abc import Iterable
from typing import Any, cast

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.relations import PKOnlyObject, PrimaryKeyRelatedField, RelatedField

from core.managers import get_current_tenant


class ChoiceField(serializers.Field[object, object, dict[str, object] | None, object]):
    """
    Represent a ChoiceField as {'value': <DB value>, 'label': <string>}. Accepts a single value on write.
    """

    # typing: third-party-untyped: DRF field constructors accept extensible keyword options
    def __init__(
        self,
        choices: Iterable[tuple[object, object]],
        allow_blank: bool = False,
        **kwargs: Any,
    ) -> None:
        self.choiceset = tuple(choices)
        self.allow_blank = allow_blank
        self._choices = dict(list(self.choiceset))

        super().__init__(**kwargs)

    # typing: third-party-untyped: DRF passes empty sentinels and parser-native values
    def validate_empty_values(self, data: Any) -> tuple[bool, Any]:
        if data is None:
            if self.allow_null:
                return True, None
            data = ""
        return super().validate_empty_values(data)

    def to_representation(self, obj: object) -> dict[str, object] | None:
        if obj != "":
            return {
                "value": obj,
                "label": self._choices.get(obj, ""),
            }
        return None

    # typing: third-party-untyped: DRF passes parser-native choice values
    def to_internal_value(self, data: Any) -> object:
        if data == "":
            if self.allow_blank:
                return data
            raise ValidationError(_("This field may not be blank."))

        if isinstance(data, (dict, list)):
            raise ValidationError(
                _('Value must be passed directly (e.g. "foo": 123); do not use a dictionary or list.')
            )

        if hasattr(data, "lower"):
            if data.lower() == "true":
                data = True
            elif data.lower() == "false":
                data = False
            else:
                try:
                    data = int(data)
                except ValueError:
                    pass

        try:
            if data in self._choices:
                return data
        except TypeError:
            pass

        raise ValidationError(_("{value} is not a valid choice.").format(value=data))

    @property
    def choices(self) -> dict[object, object]:
        return self._choices


def validate_gfk_target_tenant(content_type: ContentType | None, object_id: object | None) -> object | None:
    """Validate that a generic-FK write ``(content_type, object_id)`` targets an
    object visible within the current tenant, and return the resolved object.

    Generic-FK serializers accept an arbitrary ``content_type`` + ``object_id``
    pair. Without this check a user in tenant A can attach (contact / license /
    subscription) assignments to objects owned by tenant B simply by supplying
    that object's id (cross-tenant write / existence oracle). We resolve the
    target through the model's *default* manager (tenant-scoped for tenant-aware
    models) and additionally compare ``obj.tenant`` to the active tenant so the
    check still holds for models whose default manager is not tenant-scoped.
    Objects with no tenant (global/shared catalogue rows) are permitted.
    """

    if content_type is None or object_id is None:
        return None
    model_class = content_type.model_class()
    if model_class is None:
        raise ValidationError(_("Invalid content type."))
    target = model_class._default_manager.filter(pk=object_id).first()
    if target is None:
        raise ValidationError(_("Referenced object was not found in the current tenant."))
    tenant = get_current_tenant()
    obj_tenant = getattr(target, "tenant", None)
    if tenant is not None and obj_tenant is not None and obj_tenant != tenant:
        raise ValidationError(_("Referenced object belongs to another tenant."))
    return target


@extend_schema_field(OpenApiTypes.STR)
class ContentTypeField(RelatedField[ContentType, object, str]):
    """
    Represent a ContentType as '<app_label>.<model>'
    """

    default_error_messages = {
        "does_not_exist": _("Invalid content type: {content_type}"),
        "invalid": _("Invalid value. Specify a content type as '<app_label>.<model_name>'."),
    }

    # typing: third-party-untyped: DRF passes parser-native content-type values
    def to_internal_value(self, data: Any) -> ContentType:
        try:
            app_label, model = data.split(".")
            return ContentType.objects.get_by_natural_key(app_label=app_label, model=model)
        except ObjectDoesNotExist:
            self.fail("does_not_exist", content_type=data)
        except (AttributeError, TypeError, ValueError):
            self.fail("invalid")

    def to_representation(self, obj: ContentType | PKOnlyObject) -> str:
        obj = cast(ContentType, obj)
        return f"{obj.app_label}.{obj.model}"


class SerializedPKRelatedField(PrimaryKeyRelatedField[Model]):
    """
    Extends PrimaryKeyRelatedField to return a serialized object on read.
    """

    # typing: third-party-untyped: DRF field constructors accept extensible keyword options
    # typing: third-party-untyped: nested serializer classes have heterogeneous model parameters
    def __init__(self, serializer: type[serializers.Serializer[Any]], nested: bool = False, **kwargs: Any) -> None:
        self.serializer = serializer
        self.nested = nested
        self.pk_field = kwargs.pop("pk_field", None)
        super().__init__(**kwargs)

    # typing: third-party-untyped: DRF relation hooks may receive a PK-only proxy or arbitrary model value
    def to_representation(self, value: Model | PKOnlyObject) -> object:
        return self.serializer(value, nested=self.nested, context={"request": self.context["request"]}).data


@extend_schema_field(OpenApiTypes.INT64)
class RelatedObjectCountField(serializers.ReadOnlyField):
    """
    Represents a read-only integer count of related objects.
    """

    # typing: third-party-untyped: DRF field constructors accept extensible keyword options
    def __init__(self, relation: str, **kwargs: Any) -> None:
        self.relation = relation
        super().__init__(**kwargs)
