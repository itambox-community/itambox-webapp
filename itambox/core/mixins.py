from contextlib import contextmanager

from django.contrib.contenttypes.fields import GenericRelation
from django.db import models
from django.db.models.deletion import Collector
from django.utils.translation import gettext_lazy as _

from core.choices import ObjectChangeActionChoices
from core.slugs import generate_unique_slug
from itambox.registry import registry

_SKIP_CUSTOM_FIELD_DATA_VALIDATION = "_skip_custom_field_data_validation"


@contextmanager
def suppress_custom_field_data_validation(instance):
    """Temporarily suppress the dynamic custom-field validator on one instance.

    The marker is instance state, so it must be restored exactly: a nested or
    re-entrant suppression has to preserve an existing marker value, and an
    exception must never leave the instance in a bypassed state. This helper is
    the single place that manipulates the marker; ModelForm staging and the
    pre-save signal both use it so their semantics cannot drift apart.
    """
    had_marker = hasattr(instance, _SKIP_CUSTOM_FIELD_DATA_VALIDATION)
    previous = getattr(instance, _SKIP_CUSTOM_FIELD_DATA_VALIDATION, None)
    setattr(instance, _SKIP_CUSTOM_FIELD_DATA_VALIDATION, True)
    try:
        yield
    finally:
        if had_marker:
            setattr(instance, _SKIP_CUSTOM_FIELD_DATA_VALIDATION, previous)
        else:
            delattr(instance, _SKIP_CUSTOM_FIELD_DATA_VALIDATION)


class BookmarkableMixin:
    """
    Mixin for models that users can bookmark (star) and watch (bell) in the UI.
    Registering here sets both 'bookmarkable' (personal quick-access pin, no
    notifications) and 'watchable' (notify-on-change via ObjectWatch) features on
    the model. Every bookmarkable model is also watchable; do NOT add a second mixin.
    """

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "bookmarkable")
        registry.register_feature(cls, "watchable")


class CloneableMixin:
    """
    Mixin for models that support deep-copy via clone() method.
    """

    def clone(self):
        fields = [f for f in self._meta.concrete_fields if f.name != self._meta.pk.name]
        clone = self.__class__()
        for field in fields:
            value = getattr(self, field.name)
            if isinstance(value, models.Manager):
                continue
            setattr(clone, field.name, value)
        return clone

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "cloneable")


class CustomFieldDataMixin(models.Model):
    """
    Abstract model providing custom field value storage. Which fields apply to
    a model is declared on extras.CustomField.object_types; this mixin only
    supplies the JSON storage and registers the feature flag.
    """

    custom_field_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Custom Field Data"),
    )

    #: Fields whose limited ``save(update_fields=...)`` must still run the
    #: dynamic custom-field validator, because changing them alters which
    #: custom fields are effective for the instance (for example
    #: ``Asset.asset_type`` changes the composed fieldsets, required fields,
    #: and Choice Set validity). Concrete models declare their own set; the
    #: pre-save signal reads it generically so ``core`` stays domain-neutral.
    custom_field_data_validation_dependencies: frozenset[str] = frozenset()

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if getattr(self, _SKIP_CUSTOM_FIELD_DATA_VALIDATION, False):
            return
        validator = registry.get_custom_field_data_validator(type(self))
        if validator is not None:
            validator(self)

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "custom_field_data")


class ExportableMixin:
    """
    Mixin for models that support export templates (CSV, JSON, XML).
    """

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "exportable")


class ImportableMixin:
    """
    Mixin for models that support CSV/JSON bulk import.
    """

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "importable")


class JournalingMixin(models.Model):
    """
    Mixin for models that support persistent free-form commentary
    via JournalEntry records.
    Models using this mixin will have a reverse GenericRelation
    to JournalEntry for easy lookup.
    """

    journal_entries = GenericRelation("extras.JournalEntry", content_type_field="model", object_id_field="object_id")

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "journaling")


class ImageAttachmentMixin(models.Model):
    """
    Mixin for models that can have uploaded images.
    """

    image_attachments = GenericRelation(
        "extras.ImageAttachment", content_type_field="model", object_id_field="object_id"
    )

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "image_attachments")


class FileAttachmentMixin(models.Model):
    """
    Mixin for models that can have uploaded files.
    """

    file_attachments = GenericRelation("extras.FileAttachment", content_type_field="model", object_id_field="object_id")

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "file_attachments")


class TaggableMixin:
    """
    Mixin for models that support colored tags via a standard M2M field.
    When adopted, models should declare:
        tags = models.ManyToManyField('extras.Tag', related_name='%(app_label)s_%(class)s', blank=True)
    or equivalent.
    """

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "taggable")


class SoftDeleteMixin(models.Model):
    """
    Mixin for models that support soft deletion via a `deleted_at` timestamp.
    Objects with a non-null `deleted_at` are considered deleted.
    """

    deleted_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    class Meta:
        abstract = True

    def soft_delete(self):
        from django.utils import timezone

        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])

    def delete(self, *args, force_hard_delete=False, **kwargs):
        """
        Overrides Django's standard delete. If force_hard_delete is True,
        performs a standard physical delete (still change-logged when a
        ChangeLoggingMixin sits later in the MRO). Otherwise, soft-deletes.
        """
        # force_hard_delete may also have been stashed on the instance by
        # ChangeLoggingMixin earlier in the MRO. Resolve from either source and
        # re-stash so the partner mixin sees it regardless of MRO order; never
        # forward the kwarg to super() (models.Model.delete() rejects it).
        force_hard = force_hard_delete or getattr(self, "_force_hard_delete", False)
        self._force_hard_delete = force_hard
        if force_hard:
            super().delete(*args, **kwargs)
        else:
            from django.db import transaction

            using = self._state.db or "default"
            with transaction.atomic(using=using):
                if hasattr(self, "snapshot") and callable(self.snapshot):
                    self.snapshot()
                self._changelog_action = ObjectChangeActionChoices.ACTION_DELETE

                # Check only: PROTECT/RESTRICT children still refuse the delete
                # (ProtectedError). Nothing is written; children are retired by
                # the aggregate's archive handler (#619).
                Collector(using=using).collect([self])
                self.soft_delete()

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "soft_delete")


class AutoSlugMixin:
    """
    Mixin to automatically generate a unique slug field on save.

    By default, it will slugify the field specified by `slug_source` (default: 'name').
    It can also take a tuple/list of field names as `slug_source`, which will be
    concatenated together.

    If there is a collision, it will append a counter to ensure uniqueness.
    """

    slug_source = "name"

    def save(self, *args, **kwargs):
        if not getattr(self, "slug", None):
            generate_unique_slug(self, self.slug_source)
        super().save(*args, **kwargs)


class SubscribableMixin(models.Model):
    """
    Mixin for models that can have SaaS subscriptions assigned to them.
    Models using this mixin will have a reverse GenericRelation
    to SubscriptionAssignment.
    """

    subscriptions = GenericRelation(
        "subscriptions.SubscriptionAssignment",
        content_type_field="content_type",
        object_id_field="object_id",
        related_query_name="%(app_label)s_%(class)s",
    )

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        registry.register_feature(cls, "subscribable")
