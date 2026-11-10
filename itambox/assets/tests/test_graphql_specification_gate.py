from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase

from assets.models import Asset, AssetType, Category, CategoryDefaultFieldset, Manufacturer
from assets.models.catalog import AssetTypeFieldset
from core.schema import build_schema
from core.tests.mixins import grant
from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField
from inventory.models import Accessory, Component, Consumable
from itambox.middleware import set_current_tenant
from organization.models import AssetHolder, Role, Tenant

# The budgets are the endpoint contract; resolver gating is tested without them.
schema = build_schema(extensions=[])

BASE = ["assets.view_assettype", "inventory.view_accessory", "inventory.view_consumable", "inventory.view_component"]
FIELD = "extras.view_customfield"
CHOICES = "extras.view_customfieldchoiceset"

CATEGORY_PATHS = {
    "accessories": "query { accessories { category { defaultFieldsets { identity } } } }",
    "consumables": "query { consumables { category { defaultFieldsets { identity } } } }",
    "components": "query { components { category { defaultFieldsets { identity } } } }",
    "assetTypes": "query { assetTypes(first: 10) { edges { node { category { defaultFieldsets { identity } } } } } }",
}
TYPE_FIELDSETS = "query { assetTypes(first: 10) { edges { node { fieldsets { identity } } } } }"
DEFINITION = (
    "query { assetTypes(first: 1) { edges { node { specificationDefinition(target: ASSET_TYPE) "
    "{ sections { label fields { key } } } } } } }"
)
ENTRIES = "query { assets { specificationEntries { key definition { key } } } }"
CHOICE_SET = "query { assetTypes(first: 1) { edges { node { fieldsets { fields { key choiceSet { identity } } } } } } }"


class SpecificationGateTests(TestCase):
    """Specification definitions need the extras permissions, not the parent's (#1067)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Spec Gate Tenant", slug="spec-gate-tenant")
        set_current_tenant(self.tenant)
        self.addCleanup(set_current_tenant, None)
        manufacturer = Manufacturer.objects.create(name="Spec Gate Mfr", slug="spec-gate-mfr")
        category = Category.objects.create(
            name="Spec Gate Cat",
            slug="spec-gate-cat",
            applies_to={"asset": True, "accessory": True, "consumable": True, "component": True},
        )
        fieldset = CustomFieldset.objects.create(namespace="local", slug="gate-specs", label="Gate specs")
        choices = CustomFieldChoiceSet.objects.create(namespace="local", slug="gate-state", label="Gate state")
        CustomFieldChoice.objects.create(choice_set=choices, key="ready", label="Ready", position=1)
        field = CustomField.objects.create(
            namespace="local",
            name="gate_state",
            label="State",
            field_type=CustomField.FIELD_TYPE_SINGLE_SELECT,
            activation=CustomField.ACTIVATION_COMPOSED,
            choice_set=choices,
            max_values=1,
            nullable=True,
        )
        field.object_types.add(*ContentType.objects.get_for_models(AssetType, Asset).values())
        CustomFieldsetField.objects.create(fieldset=fieldset, custom_field=field, position=1)
        CategoryDefaultFieldset.objects.create(category=category, fieldset=fieldset, position=1)
        asset_type = AssetType.objects.create(
            manufacturer=manufacturer, model="Gate Type", slug="gate-type", category=category
        )
        AssetTypeFieldset.objects.create(asset_type=asset_type, fieldset=fieldset, position=1)
        Asset.objects.create(
            tenant=self.tenant, name="Gate Asset", asset_type=asset_type, custom_field_data={"gate_state": "ready"}
        )
        self.asset_reader = self._member("specassets", [*BASE, "assets.view_asset"])
        self.asset_reader_fields = self._member("specassetsf", [*BASE, "assets.view_asset", FIELD])
        kwargs = {"manufacturer": manufacturer, "category": category, "tenant": self.tenant}
        Accessory.objects.create(name="Gate Acc", **kwargs)
        Consumable.objects.create(name="Gate Con", **kwargs)
        Component.objects.create(name="Gate Comp", **kwargs)
        self.restricted = self._member("specrestricted", BASE)
        self.fields_only = self._member("specfields", [*BASE, FIELD])
        self.full = self._member("specfull", [*BASE, FIELD, CHOICES])

    def _member(self, username, permissions):
        user = get_user_model().objects.create_user(username=username, email=f"{username}@example.com", password="x")
        AssetHolder.objects.create(
            user=user, first_name=username, last_name="User", upn=username, email=user.email, tenant=self.tenant
        )
        role = Role.objects.create(tenant=self.tenant, name=f"{username} role", permissions=permissions)
        grant(user, self.tenant, role)
        return user

    def _run(self, query, user):
        request = RequestFactory().post("/graphql")
        request.user = get_user_model().objects.get(pk=user.pk)
        request.active_tenant = self.tenant
        result = schema.execute_sync(query, context_value=request)
        self.assertIsNone(result.errors)
        return result.data

    @staticmethod
    def _category(data, root):
        rows = data[root]["edges"] if root == "assetTypes" else data[root]
        rows = [r["node"] if "node" in r else r for r in rows]
        self_rows = [r["category"]["defaultFieldsets"] for r in rows]
        assert len(self_rows) == 1
        return self_rows[0]

    def test_category_default_fieldsets_empty_without_custom_field_permission(self):
        for root, query in CATEGORY_PATHS.items():
            with self.subTest(root=root):
                self.assertEqual(self._category(self._run(query, self.restricted), root), [])

    def test_category_default_fieldsets_visible_with_custom_field_permission(self):
        for root, query in CATEGORY_PATHS.items():
            with self.subTest(root=root):
                got = self._category(self._run(query, self.fields_only), root)
                self.assertEqual(got, [{"identity": "local/gate-specs"}])

    def test_asset_type_fieldsets_empty_without_custom_field_permission(self):
        edges = self._run(TYPE_FIELDSETS, self.restricted)["assetTypes"]["edges"]
        self.assertEqual(edges[0]["node"]["fieldsets"], [])

    def test_asset_type_fieldsets_visible_with_custom_field_permission(self):
        edges = self._run(TYPE_FIELDSETS, self.fields_only)["assetTypes"]["edges"]
        self.assertEqual(edges[0]["node"]["fieldsets"], [{"identity": "local/gate-specs"}])

    def test_choice_set_null_without_choice_set_permission(self):
        edges = self._run(CHOICE_SET, self.fields_only)["assetTypes"]["edges"]
        fields = edges[0]["node"]["fieldsets"][0]["fields"]
        self.assertEqual([f["key"] for f in fields], ["gate_state"])
        self.assertIsNone(fields[0]["choiceSet"])

    def test_choice_set_visible_with_choice_set_permission(self):
        edges = self._run(CHOICE_SET, self.full)["assetTypes"]["edges"]
        fields = edges[0]["node"]["fieldsets"][0]["fields"]
        self.assertEqual(fields[0]["choiceSet"], {"identity": "local/gate-state"})

    def test_definition_sections_empty_without_custom_field_permission(self):
        edges = self._run(DEFINITION, self.restricted)["assetTypes"]["edges"]
        self.assertEqual(edges[0]["node"]["specificationDefinition"]["sections"], [])

    def test_definition_sections_visible_with_custom_field_permission(self):
        edges = self._run(DEFINITION, self.fields_only)["assetTypes"]["edges"]
        sections = edges[0]["node"]["specificationDefinition"]["sections"]
        self.assertEqual([f["key"] for s in sections for f in s["fields"]], ["gate_state"])

    def test_entry_definition_null_without_custom_field_permission(self):
        rows = self._run(ENTRIES, self.asset_reader)["assets"]
        entries = rows[0]["specificationEntries"]
        self.assertEqual([e["key"] for e in entries], ["gate_state"])
        self.assertIsNone(entries[0]["definition"])

    def test_entry_definition_visible_with_custom_field_permission(self):
        rows = self._run(ENTRIES, self.asset_reader_fields)["assets"]
        entries = rows[0]["specificationEntries"]
        self.assertEqual(entries[0]["definition"], {"key": "gate_state"})
