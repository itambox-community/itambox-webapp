"""Model behavior shared by the checkable inventory catalogue models."""

from django.db import models
from django.urls import NoReverseMatch, reverse

from core.managers import Scope


class CheckableInventoryModelMixin(models.Model):
    """
    Mixin for inventory models (Component, Accessory, Consumable) to support unified
    checking-out workflows, active assignment resolution, and absolute action URL lookups.
    """

    class Meta:
        abstract = True

    @property
    def checkout_url(self) -> str:
        """
        Dynamically returns the checkout URL name/path based on the model's namespace
        and view name convention (e.g. inventory:accessory_checkout).
        """
        app_label = self._meta.app_label
        model_name = self._meta.model_name
        try:
            return reverse(f"{app_label}:{model_name}_checkout", kwargs={"pk": self.pk})
        except NoReverseMatch:
            return ""

    @property
    def active_assignments(self) -> models.QuerySet:
        """
        Resolves the active assignment/allocation/consumption queryset for this model instance
        regardless of the varying reverse relationship names (assignments vs allocations vs consumptions).
        """
        scope = Scope.current()
        if hasattr(self, "assignments"):
            return self.assignments.all().for_scope(scope)
        elif hasattr(self, "allocations"):
            # Soft-deletable component allocations
            return self.allocations.filter(deleted_at__isnull=True).for_scope(scope)
        elif hasattr(self, "consumptions"):
            return self.consumptions.all().for_scope(scope)
        return None

    @property
    def active_assignments_count(self) -> int:
        """
        Returns the count of active assignments, allocations, or consumptions.
        """
        qs = self.active_assignments
        return qs.count() if qs is not None else 0
