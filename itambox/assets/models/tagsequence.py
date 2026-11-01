"""AssetTagSequence — tenant-scoped auto-numbering sequences for asset tags."""

import time

from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from core.managers import ExplicitScopeAllObjectsManager, ExplicitScopeSoftDeleteManager
from core.mixins import SoftDeleteMixin
from core.models import BaseModel, ChangeLoggingMixin


class AssetTagSequence(ChangeLoggingMixin, BaseModel, SoftDeleteMixin):
    objects = ExplicitScopeSoftDeleteManager()
    all_objects = ExplicitScopeAllObjectsManager()
    allow_global_tenant = True

    tenant = models.ForeignKey(
        "organization.Tenant",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="tag_sequences",
        db_index=True,
        verbose_name=_("Tenant"),
        help_text=_("The tenant owning this sequence. Null represents system-wide/global sequences."),
    )
    category = models.ForeignKey(
        "assets.Category",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="tag_sequences",
        db_index=True,
        verbose_name=_("Category"),
        help_text=_("The asset category this sequence applies to. Null represents default sequences."),
    )
    prefix = models.CharField(
        max_length=20,
        default="ASSET-",
        verbose_name=_("Prefix"),
        help_text=_("Prefix for generated asset tags (e.g. ASSET-)"),
    )
    next_value = models.PositiveIntegerField(default=1, verbose_name=_("Next Value"))
    zero_padding = models.PositiveSmallIntegerField(default=6, verbose_name=_("Zero Padding"))
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))

    archive_operation_id = models.UUIDField(null=True, blank=True, editable=False, verbose_name=_("Archive Operation"))

    class Meta:
        verbose_name = _("Asset Tag Sequence")
        verbose_name_plural = _("Asset Tag Sequences")
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "prefix"],
                condition=models.Q(tenant__isnull=False) & models.Q(deleted_at__isnull=True),
                name="unique_tenant_prefix",
            ),
            models.UniqueConstraint(
                fields=["prefix"],
                condition=models.Q(tenant__isnull=True) & models.Q(deleted_at__isnull=True),
                name="unique_global_prefix",
            ),
        ]

    def __str__(self):
        return f"{self.prefix} (next: {self.next_value:0{self.zero_padding}d})"

    def get_absolute_url(self):
        return reverse("assets:assettagsequence_detail", kwargs={"pk": self.pk})

    def next_tag(self):

        # Lock the sequence row before reading: formatting the tag from an unlocked
        # read lets two concurrent saves claim the same value and collide on the
        # asset_tag unique constraint. The live-row predicate is repeated under
        # the lock so an archive committed while this caller waited cannot allocate.
        with transaction.atomic():
            locked = type(self)._base_manager.select_for_update().get(pk=self.pk, deleted_at__isnull=True)
            tag = f"{locked.prefix}{locked.next_value:0{locked.zero_padding}d}"
            type(self)._base_manager.filter(pk=self.pk).update(next_value=F("next_value") + 1)
        self.refresh_from_db(fields=["next_value"])
        return tag

    @property
    def next_tag_preview(self):
        return f"{self.prefix}{self.next_value:0{self.zero_padding}d}"

    @classmethod
    def _get_or_create_global_default(cls):
        """
        Returns the global default sequence (no tenant, no category, prefix='ASSET-'),
        creating it on first use.

        get_or_create's check-then-create is not atomic: two concurrent first-ever
        blank-tag, no-tenant assets can both take the create branch and race the
        unique_global_prefix constraint -> IntegrityError. Absorb that race by
        re-selecting the row the other writer committed.

        The winner's insert may still be invisible under READ COMMITTED when the
        race happens inside an outer ``transaction.atomic()`` block (e.g. a
        custody accept), so the re-select retries briefly instead of raising a
        hard 500 (hit on #306: parallel custody accepts on a fresh instance).
        """

        try:
            seq, _ = cls._base_manager.get_or_create(
                tenant__isnull=True,
                category__isnull=True,
                prefix="ASSET-",
                deleted_at__isnull=True,
                defaults={"next_value": 1, "zero_padding": 6, "is_active": True},
            )
            return seq
        except IntegrityError:
            # A parallel writer won the unique_global_prefix race; re-select
            # its row with a short bounded retry.
            return cls._select_global_default_with_retry()

    @classmethod
    def _select_global_default_with_retry(cls):
        """Re-select the global default row the winner of the race committed."""
        for _attempt in range(5):
            try:
                return cls._base_manager.get(
                    tenant__isnull=True, category__isnull=True, prefix="ASSET-", deleted_at__isnull=True
                )
            except cls.DoesNotExist:
                time.sleep(0.05)
        # No row after the retry bound: surface the real DoesNotExist.
        return cls._base_manager.get(
            tenant__isnull=True, category__isnull=True, prefix="ASSET-", deleted_at__isnull=True
        )

    @classmethod
    def get_next_tag_for_asset(cls, asset):
        """Allocate from the first eligible sequence, retrying if it is archived under lock."""
        for _attempt in range(5):
            try:
                return cls._allocate_next_tag_for_asset(asset)
            except cls.DoesNotExist:
                continue
        raise cls.DoesNotExist("No live asset tag sequence remained available during allocation.")

    @classmethod
    def _allocate_next_tag_for_asset(cls, asset):
        """
        Resolves the next asset tag for the given asset based on a hierarchical fallback chain:
        1. Tenant-specific + Category-specific sequence
        2. Tenant-specific default sequence (no category)
        3. Global + Category-specific sequence
        4. Global default sequence (prefix='ASSET-', created if missing)
        """
        # 1. Tenant + Category specific
        if asset.tenant_id and asset.category:
            seq = cls._base_manager.filter(
                tenant_id=asset.tenant_id, category=asset.category, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq.next_tag()

        # 2. Tenant default (no category)
        if asset.tenant_id:
            seq = cls._base_manager.filter(
                tenant_id=asset.tenant_id, category__isnull=True, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq.next_tag()

        # 3. Global + Category specific
        if asset.category:
            seq = cls._base_manager.filter(
                tenant__isnull=True, category=asset.category, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq.next_tag()

        # 4. Global default (no tenant, no category, prefix='ASSET-')
        return cls._get_or_create_global_default().next_tag()

    @classmethod
    def resolve_sequence_for_asset(cls, asset):
        """
        Resolves the matching sequence object for the asset based on the fallback chain.
        Does not increment or modify the sequence.
        """
        # 1. Tenant + Category specific
        if asset.tenant_id and asset.category:
            seq = cls._base_manager.filter(
                tenant_id=asset.tenant_id, category=asset.category, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq

        # 2. Tenant default (no category)
        if asset.tenant_id:
            seq = cls._base_manager.filter(
                tenant_id=asset.tenant_id, category__isnull=True, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq

        # 3. Global + Category specific
        if asset.category:
            seq = cls._base_manager.filter(
                tenant__isnull=True, category=asset.category, is_active=True, deleted_at__isnull=True
            ).first()
            if seq:
                return seq

        # 4. Global default (no tenant, no category, prefix='ASSET-')
        return cls._get_or_create_global_default()
