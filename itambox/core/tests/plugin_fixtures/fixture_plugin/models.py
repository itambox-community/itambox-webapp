from django.db import models


class FixtureWidget(models.Model):
    tenant = models.ForeignKey("organization.Tenant", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)

    class Meta:
        verbose_name = "Fixture widget"
        verbose_name_plural = "Fixture widgets"
