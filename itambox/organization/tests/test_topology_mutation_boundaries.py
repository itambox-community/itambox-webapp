from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework.test import APITestCase

from core.tests.mixins import grant
from organization.api.serializers import TenantSerializer
from organization.forms import TenantForm
from organization.models import Role, Tenant, TenantGroup

User = get_user_model()


class TopologyMutationBoundaryTests(APITestCase):
    def setUp(self):
        self.group = TenantGroup.objects.create(
            name="Topology group",
            slug="topology-group",
        )
        self.other_group = TenantGroup.objects.create(
            name="Other topology group",
            slug="other-topology-group",
        )
        self.tenant = Tenant.objects.create(
            name="Topology tenant",
            slug="topology-tenant",
            group=self.group,
        )
        self.provider = Tenant.objects.create(
            name="Forbidden provider",
            slug="forbidden-provider",
            is_provider=True,
        )
        self.user = User.objects.create_user(
            username="topology-operator",
            password="password",
            is_staff=True,
        )
        self.role = Role.objects.create(
            tenant=self.tenant,
            name="Topology operator",
            permissions=[
                "organization.view_tenant",
                "organization.add_tenant",
                "organization.change_tenant",
                "organization.change_role",
                "organization.view_tenantgroup",
                "organization.add_tenantgroup",
                "organization.change_tenantgroup",
                "organization.delete_tenantgroup",
                "core.change_recyclebin",
                "core.delete_recyclebin",
            ],
        )
        self.membership = grant(self.user, self.tenant, self.role).membership
        self.superuser = User.objects.create_superuser(
            username="topology-superuser",
            password="password",
        )

    def login_operator(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["active_tenant_id"] = self.tenant.pk
        session.save()

    @staticmethod
    def etag(obj):
        return f'W/"{obj.updated_at.isoformat()}"'

    def test_tenant_group_ui_reads_remain_available_but_mutations_are_denied(self):
        self.login_operator()

        list_response = self.client.get(reverse("organization:tenantgroup_list"))
        detail_response = self.client.get(
            reverse(
                "organization:tenantgroup_detail",
                kwargs={"pk": self.group.pk},
            )
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertFalse(list_response.context["can_add"])
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response.context["action_urls"],
            # A tenant group carries no label, so the label target stays empty.
            {"edit": None, "delete": None, "clone": None, "label": None},
        )
        self.assertIsNone(list_response.context["import_url"])
        self.assertIsNone(list_response.context["bulk_edit_url"])
        self.assertIsNone(list_response.context["bulk_delete_url"])

        create_response = self.client.post(
            reverse("organization:tenantgroup_create"),
            {"name": "Forbidden group", "slug": "forbidden-group"},
        )
        update_response = self.client.post(
            reverse("organization:tenantgroup_update", kwargs={"pk": self.group.pk}),
            {"name": "Forbidden rename", "slug": self.group.slug},
        )
        delete_response = self.client.post(
            reverse("organization:tenantgroup_delete", kwargs={"pk": self.group.pk}),
        )

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(update_response.status_code, 403)
        self.assertEqual(delete_response.status_code, 403)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Topology group")
        self.assertFalse(TenantGroup.objects.filter(name="Forbidden group").exists())

    def test_tenant_group_generic_bulk_import_and_recycle_mutations_are_denied(self):
        self.login_operator()
        bulk_payload = {
            "pk": [self.group.pk],
            "model_name": "organization.tenantgroup",
            "return_url": reverse("organization:tenantgroup_list"),
        }
        self.assertEqual(
            self.client.post(reverse("bulk_edit"), bulk_payload).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("bulk_delete"), bulk_payload).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "generic_import",
                    kwargs={
                        "app_label": "organization",
                        "model_name": "tenantgroup",
                    },
                )
            ).status_code,
            # Not an allowlisted import model: the route does not exist for anyone.
            404,
        )

        deleted_group = TenantGroup.objects.create(
            name="Deleted topology group",
            slug="deleted-topology-group",
        )
        deleted_group.delete()
        content_type = ContentType.objects.get_for_model(TenantGroup)
        restore_url = reverse(
            "object_restore",
            kwargs={
                "content_type_id": content_type.pk,
                "object_id": deleted_group.pk,
            },
        )
        self.assertEqual(self.client.post(restore_url).status_code, 403)
        deleted_group.refresh_from_db()
        self.assertIsNotNone(deleted_group.deleted_at)

    def test_tenant_group_api_reads_remain_available_but_mutations_are_denied(self):
        self.login_operator()
        list_url = reverse("api:organization_api:tenantgroup-list")
        detail_url = reverse(
            "api:organization_api:tenantgroup-detail",
            kwargs={"pk": self.group.pk},
        )

        self.assertEqual(self.client.get(list_url).status_code, 200)
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.assertEqual(
            self.client.post(
                list_url,
                {"name": "Forbidden API group", "slug": "forbidden-api-group"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                detail_url,
                {"name": "Forbidden API rename"},
                format="json",
                HTTP_IF_MATCH=self.etag(self.group),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                detail_url,
                HTTP_IF_MATCH=self.etag(self.group),
            ).status_code,
            403,
        )

        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Topology group")
        self.assertFalse(TenantGroup.objects.filter(name="Forbidden API group").exists())

    def test_superuser_can_mutate_tenant_groups_through_api(self):
        self.client.force_login(self.superuser)
        list_url = reverse("api:organization_api:tenantgroup-list")

        create_response = self.client.post(
            list_url,
            {"name": "Superuser API group", "slug": "superuser-api-group"},
            format="json",
        )
        self.assertEqual(create_response.status_code, 201)
        detail_url = reverse(
            "api:organization_api:tenantgroup-detail",
            kwargs={"pk": create_response.data["id"]},
        )
        update_response = self.client.patch(
            detail_url,
            {"name": "Superuser API group renamed"},
            format="json",
            HTTP_IF_MATCH=create_response["ETag"],
        )
        self.assertEqual(update_response.status_code, 200)
        delete_response = self.client.delete(
            detail_url,
            HTTP_IF_MATCH=update_response["ETag"],
        )
        self.assertEqual(delete_response.status_code, 204)

    def test_tenant_form_preserves_group_for_non_superuser(self):
        form = TenantForm(
            data={
                "name": "Topology tenant renamed",
                "slug": self.tenant.slug,
                "group": self.other_group.pk,
                "currency": "EUR",
            },
            instance=self.tenant,
            user=self.user,
        )

        self.assertNotIn("group", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.group)

        create_form = TenantForm(
            data={
                "name": "New ungrouped tenant",
                "slug": "new-ungrouped-tenant",
                "group": self.other_group.pk,
                "currency": "EUR",
            },
            user=self.user,
        )
        self.assertTrue(create_form.is_valid(), create_form.errors)
        self.assertIsNone(create_form.save().group)

    def test_tenant_ui_preserves_group_for_non_superuser(self):
        self.login_operator()
        response = self.client.post(
            reverse("organization:tenant_update", kwargs={"pk": self.tenant.pk}),
            {
                "name": "Tenant renamed in UI",
                "slug": self.tenant.slug,
                "group": self.other_group.pk,
                "currency": "EUR",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.name, "Tenant renamed in UI")
        self.assertEqual(self.tenant.group, self.group)

    def test_tenant_bulk_edit_preserves_superuser_only_topology_fields(self):
        self.login_operator()
        response = self.client.post(
            reverse("organization:tenant_bulk_edit"),
            {
                "pk": [self.tenant.pk],
                "_selected_fields": ["group", "is_provider", "managed_by"],
                "group": self.other_group.pk,
                "is_provider": "on",
                "managed_by": self.provider.pk,
                "_apply": "1",
                "return_url": reverse("organization:tenant_list"),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.group)
        self.assertFalse(self.tenant.is_provider)
        self.assertIsNone(self.tenant.managed_by)

    def test_global_dynamic_bulk_edit_cannot_bypass_tenant_topology_form(self):
        self.login_operator()

        response = self.client.post(
            reverse("bulk_edit"),
            {
                "pk": [self.tenant.pk],
                "model_name": "organization.tenant",
                "_selected_fields": ["group", "is_provider", "managed_by"],
                "group": self.other_group.pk,
                "is_provider": "on",
                "managed_by": self.provider.pk,
                "_apply": "1",
                "return_url": reverse("organization:tenant_list"),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.group)
        self.assertFalse(self.tenant.is_provider)
        self.assertIsNone(self.tenant.managed_by)

    def test_global_dynamic_bulk_edit_cannot_bypass_role_form(self):
        target_role = Role.objects.create(
            tenant=self.tenant,
            name="Retained projected role",
            permissions=["organization.view_tenant"],
            shared_with_managed=False,
        )
        self.login_operator()

        response = self.client.post(
            reverse("bulk_edit"),
            {
                "pk": [target_role.pk],
                "model_name": "organization.role",
                "_selected_fields": ["tenant", "shared_with_managed"],
                "tenant": self.provider.pk,
                "shared_with_managed": "on",
                "_apply": "1",
                "return_url": reverse("organization:role_list"),
            },
        )

        self.assertEqual(response.status_code, 403)
        target_role.refresh_from_db()
        self.assertEqual(target_role.tenant, self.tenant)
        self.assertFalse(target_role.shared_with_managed)

    def test_generic_tenant_import_is_not_available_to_anyone(self):
        # Tenant topology (group, managed_by, is_provider) is written only through
        # TenantForm and the API; the generic importer has no tenant form at all.
        import_url = reverse(
            "generic_import",
            kwargs={
                "app_label": "organization",
                "model_name": "tenant",
            },
        )
        self.login_operator()
        self.assertEqual(self.client.get(import_url).status_code, 404)

        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(import_url).status_code, 404)

    def test_tenant_api_rejects_group_for_non_superuser(self):
        self.login_operator()
        detail_url = reverse(
            "api:organization_api:tenant-detail",
            kwargs={"pk": self.tenant.pk},
        )
        response = self.client.patch(
            detail_url,
            {"group_id": self.other_group.pk},
            format="json",
            HTTP_IF_MATCH=self.etag(self.tenant),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("group_id", response.data)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.group)

        list_url = reverse("api:organization_api:tenant-list")
        create_response = self.client.post(
            list_url,
            {
                "name": "Forbidden grouped tenant",
                "slug": "forbidden-grouped-tenant",
                "group_id": self.other_group.pk,
            },
            format="json",
        )
        self.assertEqual(create_response.status_code, 400)
        self.assertFalse(Tenant.objects.filter(slug="forbidden-grouped-tenant").exists())

    def test_tenant_api_does_not_expose_management_tree_fields(self):
        self.assertNotIn("is_provider", TenantSerializer.Meta.fields)
        self.assertNotIn("managed_by", TenantSerializer.Meta.fields)

        self.login_operator()
        response = self.client.get(
            reverse(
                "api:organization_api:tenant-detail",
                kwargs={"pk": self.tenant.pk},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("is_provider", response.data)
        self.assertNotIn("managed_by", response.data)

    def test_superuser_can_change_tenant_group_in_form_and_api(self):
        form = TenantForm(
            data={
                "name": self.tenant.name,
                "slug": self.tenant.slug,
                "group": self.other_group.pk,
                "currency": "EUR",
            },
            instance=self.tenant,
            user=self.superuser,
        )
        self.assertIn("group", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.other_group)

        self.client.force_login(self.superuser)
        detail_url = reverse(
            "api:organization_api:tenant-detail",
            kwargs={"pk": self.tenant.pk},
        )
        response = self.client.patch(
            detail_url,
            {"group_id": self.group.pk},
            format="json",
            HTTP_IF_MATCH=self.etag(self.tenant),
        )
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.group, self.group)
