"""B6 regression: API update must not let a non-superuser re-tenant a row.

`perform_update` lacked the create-time tenant re-pin, so a non-superuser PATCH of
`tenant_id=null` on an allow_global_tenant model (Software) globalized the row
(tenant=None), exposing it to every tenant. perform_update now pins the tenant
back to the object's existing tenant for non-superusers.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

# Import the API view module at collection time (no tenant context) so its
# `queryset = Software.objects...all()` class attribute bakes UNSCOPED — see the
# note in inventory/tests/test_checkout_permissions.py for the failure mode.
import software.api.views  # noqa: F401,E402
from assets.models import Manufacturer
from core.managers import set_current_tenant
from core.tests.mixins import grant
from organization.models import Role, Tenant
from software.models import Software

User = get_user_model()


class SoftwareApiTenantRepinTests(APITestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.user = User.objects.create_user(username="member_a", password="pw")
        self.role = Role.objects.create(
            tenant=self.tenant_a,
            name="Role A",
            permissions=["software.view_software", "software.change_software"],
        )
        grant(self.user, self.tenant_a, self.role)

        self.mfr = Manufacturer.objects.create(name="MS", slug="ms")
        self.software = Software.objects.create(name="Office", manufacturer=self.mfr, tenant=self.tenant_a)

        # Drive the active tenant deterministically via ?switch_tenant= (the
        # member's only membership) rather than relying on session persistence,
        # which is fragile across this suite's test ordering.
        self.url = (
            reverse("api:software_api:software-detail", kwargs={"pk": self.software.pk})
            + f"?switch_tenant={self.tenant_a.pk}"
        )

    def _activate(self):
        self.client.force_login(self.user)

    def test_patch_tenant_null_does_not_globalize(self):
        self._activate()
        etag = 'W/"%s"' % self.software.updated_at.isoformat()
        resp = self.client.patch(self.url, {"tenant_id": None}, format="json", HTTP_IF_MATCH=etag)
        self.assertEqual(resp.status_code, 200, resp.content)

        # Re-read under the tenant context (allow_global makes both the tenant and
        # any globalized row visible, so this query catches the bug either way).
        set_current_tenant(self.tenant_a)
        refreshed = Software.objects.get(pk=self.software.pk)
        self.assertEqual(refreshed.tenant_id, self.tenant_a.pk)
