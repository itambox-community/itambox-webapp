"""Catalog seed mixin: tenant-agnostic reference data.

Designed to be mixed into ``Command`` in seed_data.py:

    from core.management.commands._seed.catalog import SeedCatalogMixin

    class Command(SeedCatalogMixin, BaseCommand):
        ...

``_seed_catalog`` runs first; it populates ``self._status_labels``,
``self._tags``, ``self._asset_roles``, ``self._manufacturers``,
``self._suppliers``, ``self._depreciations``, ``self._demo_depreciation_afa``,
``self._custom_fields``, the fieldset handles, ``self._categories``,
``self._asset_types``, ``self._components``, ``self._accessory_defs`` /
``self._consumable_defs`` (consumed later by the stock phase), and ``self._software``.
It stores the SaaS supplier definitions for ``_seed_organizations`` to create
after tenant and tenant-group scopes are available. It reads
``self._status_label_defs()`` from Command.
"""

from decimal import Decimal

from django.apps import apps

from assets.models import AssetTypeFieldset, CategoryDefaultFieldset
from assets.services.specifications.core_vocabulary_translations import seed_core_vocabulary_translations
from extras.models import CustomField, CustomFieldChoice, CustomFieldChoiceSet, CustomFieldset, CustomFieldsetField, Tag
from extras.services.custom_field_data import write_custom_field_data


def _get_core_choice_set(slug, label):
    matches = list(CustomFieldChoiceSet.objects.filter(namespace="itambox", slug=slug))
    if len(matches) > 1:
        raise ValueError(f"Ambiguous core Choice Set identity: itambox/{slug}")
    if matches and (
        matches[0].namespace != "itambox"
        or matches[0].management_kind != CustomFieldChoiceSet.MANAGEMENT_CORE
        or matches[0].lifecycle != CustomFieldChoiceSet.LIFECYCLE_ACTIVE
    ):
        raise ValueError(f"Core Choice Set identity has incompatible management or lifecycle: itambox/{slug}")
    if matches:
        return matches[0]
    return CustomFieldChoiceSet.objects.create(
        namespace="itambox",
        slug=slug,
        label=label,
        management_kind=CustomFieldChoiceSet.MANAGEMENT_CORE,
        version=1,
        lifecycle=CustomFieldChoiceSet.LIFECYCLE_ACTIVE,
    )


def _validate_core_choice_row(choice, slug, desired_choices):
    desired = desired_choices.get(choice.key)
    if desired is None:
        raise ValueError(f"Core Choice identity is unexpected: itambox/{slug}#{choice.key}")
    if (
        desired["lifecycle"] == CustomFieldChoice.LIFECYCLE_ACTIVE
        and choice.lifecycle != CustomFieldChoice.LIFECYCLE_ACTIVE
    ):
        raise ValueError(f"Core Choice lifecycle collision: itambox/{slug}#{choice.key}")


def _reconcile_core_choice_rows(choice_set, slug, choices):
    existing_choices = list(CustomFieldChoice.objects.filter(choice_set=choice_set).order_by("position", "key"))
    if len(existing_choices) > 64:
        raise ValueError(f"Core Choice Set has more than 64 choices: itambox/{slug}")
    desired_choices = {choice["key"]: choice for choice in choices}
    for choice in existing_choices:
        _validate_core_choice_row(choice, slug, desired_choices)
    for rank, choice in enumerate(existing_choices, start=1):
        CustomFieldChoice.objects.filter(pk=choice.pk).update(position=900000 + rank)
    existing_by_key = {choice.key: choice for choice in existing_choices}
    for choice_data in choices:
        choice = existing_by_key.get(choice_data["key"])
        if choice is None:
            CustomFieldChoice.objects.create(
                choice_set=choice_set,
                key=choice_data["key"],
                label=choice_data["label"],
                position=choice_data["position"],
                version=1,
                lifecycle=choice_data["lifecycle"],
            )
            continue
        choice.label = choice_data["label"]
        choice.position = choice_data["position"]
        choice.version = 1
        choice.lifecycle = choice_data["lifecycle"]
        choice.save(update_fields=["label", "position", "version", "lifecycle"])


def _reconcile_core_choice_set(choice_set_data):
    choice_sets = {}
    for choice_set_data_row in choice_set_data:
        identity = choice_set_data_row["identity"]
        slug = choice_set_data_row["slug"]
        choice_set = _get_core_choice_set(slug, choice_set_data_row["label"])
        choice_set.label = choice_set_data_row["label"]
        choice_set.management_kind = CustomFieldChoiceSet.MANAGEMENT_CORE
        choice_set.version = 1
        choice_set.lifecycle = choice_set_data_row["lifecycle"]
        choice_set.save(update_fields=["label", "management_kind", "version", "lifecycle"])
        _reconcile_core_choice_rows(choice_set, slug, choice_set_data_row["choices"])
        choice_sets[identity] = choice_set
    return choice_sets


def _validate_core_field_identity(matches, key, lifecycle):
    if len(matches) > 1:
        raise ValueError(f"Ambiguous core field identity: {key}")
    if matches and (matches[0].namespace != "itambox" or matches[0].management_kind != CustomField.MANAGEMENT_CORE):
        raise ValueError(f"Core field identity has incompatible namespace or management: {key}")
    if matches and lifecycle == CustomField.LIFECYCLE_ACTIVE and matches[0].lifecycle != CustomField.LIFECYCLE_ACTIVE:
        raise ValueError(f"Core field lifecycle collision: {key}")


def _core_field_options(row, choice_sets, version):
    validation = row.get("validation", {})
    options = {
        "namespace": row["namespace"],
        "label": row["label"],
        "help_text": row["help_text"],
        "activation": row["activation"],
        "required": row["required"],
        "nullable": row["nullable"],
        "management_kind": CustomField.MANAGEMENT_CORE,
        "version": version,
        "lifecycle": row["lifecycle"],
        "mappings": [],
        "field_type": row["field_type"],
        "quantity_kind": row["quantity_kind"],
        "canonical_unit": row["canonical_unit"],
        "minimum_value": validation.get("minimum"),
        "maximum_value": validation.get("maximum"),
        "regex": validation.get("regex"),
        "decimal_scale": validation.get("scale"),
        "max_values": validation.get("max_values"),
        "text_max_length": validation.get("max_length"),
        "validation_rule": validation.get("rule"),
        "choice_set": choice_sets.get(row["choice_set"]) if row["choice_set"] else None,
    }
    for key in ("minimum_value", "maximum_value"):
        if options[key] is not None:
            options[key] = Decimal(options[key])
    return options


def _core_field_target_types(row, content_types):
    try:
        return [content_types[target] for target in row["targets"]]
    except KeyError as exc:
        raise ValueError(f"Unsupported core field target: {row['identity']}") from exc


def _update_existing_core_field(field, row, options):
    immutable_fields = (
        "name",
        "namespace",
        "field_type",
        "activation",
        "quantity_kind",
        "canonical_unit",
        "minimum_value",
        "maximum_value",
        "regex",
        "decimal_scale",
        "max_values",
        "text_max_length",
        "validation_rule",
        "nullable",
        "choice_set_id",
    )
    for key in immutable_fields:
        desired = (
            row["key"]
            if key == "name"
            else (options["choice_set"].pk if key == "choice_set_id" and options["choice_set"] else options.get(key))
        )
        current = field.choice_set_id if key == "choice_set_id" else getattr(field, key)
        if current != desired:
            raise ValueError(f"Core field semantics differ for identity: {row['key']}")
    mutable_fields = (
        "label",
        "help_text",
        "required",
        "mappings",
        "management_kind",
        "version",
        "lifecycle",
    )
    for key in mutable_fields:
        setattr(field, key, options[key])
    CustomField.objects.filter(pk=field.pk).update(**{key: options[key] for key in mutable_fields})


def _reconcile_core_fields(field_rows, choice_sets, asset_ct, assettype_ct, version):
    custom_fields = {}
    content_types = {"asset": asset_ct, "asset_type": assettype_ct}
    for row in field_rows:
        options = _core_field_options(row, choice_sets, version)
        target_content_types = _core_field_target_types(row, content_types)
        matches = list(CustomField.objects.filter(name=row["key"]))
        _validate_core_field_identity(matches, row["key"], row["lifecycle"])
        definition = CustomField(name=row["key"], **options)
        definition.validate_definition_contract(object_types=target_content_types)
        field = matches[0] if matches else definition
        if not matches:
            # The explicit contract check above guards this bulk path; the
            # generic pre-save signal still reflects older definition rules.
            CustomField.objects.bulk_create([field])
        else:
            _update_existing_core_field(field, row, options)
        field.object_types.set(target_content_types)
        custom_fields[row["identity"]] = field
    return custom_fields


def _get_core_fieldset(slug, label, description, lifecycle, version, namespace):
    matches = list(CustomFieldset.objects.filter(namespace=namespace, slug=slug))
    if len(matches) > 1:
        raise ValueError(f"Ambiguous core fieldset identity: {namespace}/{slug}")
    if matches and (
        matches[0].namespace != namespace
        or matches[0].management_kind != CustomFieldset.MANAGEMENT_CORE
        or matches[0].lifecycle not in {CustomFieldset.LIFECYCLE_ACTIVE, CustomFieldset.LIFECYCLE_DEPRECATED}
    ):
        raise ValueError(f"Core fieldset identity has incompatible management or lifecycle: {namespace}/{slug}")
    if (
        matches
        and lifecycle == CustomFieldset.LIFECYCLE_ACTIVE
        and matches[0].lifecycle != CustomFieldset.LIFECYCLE_ACTIVE
    ):
        raise ValueError(f"Core fieldset lifecycle collision: {namespace}/{slug}")
    if matches:
        return matches[0]
    return CustomFieldset.objects.create(
        namespace=namespace,
        slug=slug,
        label=label,
        description=description,
        management_kind=CustomFieldset.MANAGEMENT_CORE,
        version=version,
        lifecycle=lifecycle,
    )


def _reconcile_core_fieldsets(section_rows, custom_fields, version):
    fieldsets = {}
    for section in section_rows:
        namespace = section["namespace"]
        slug = section["slug"]
        fieldset = _get_core_fieldset(
            slug,
            section["label"],
            section["description"],
            section["lifecycle"],
            version,
            namespace,
        )
        fieldset.namespace = namespace
        fieldset.slug = slug
        fieldset.label = section["label"]
        fieldset.description = section["description"]
        fieldset.management_kind = CustomFieldset.MANAGEMENT_CORE
        fieldset.version = version
        fieldset.lifecycle = section["lifecycle"]
        fieldset.save()
        existing_memberships = list(fieldset.field_memberships.select_related("custom_field"))
        if any(
            membership.custom_field.management_kind != CustomField.MANAGEMENT_CORE
            for membership in existing_memberships
        ):
            raise ValueError(f"Core fieldset membership ownership collision: {namespace}/{slug}")
        expected_memberships = {member["field"].rsplit("/", 1)[1] for member in section["memberships"]}
        for membership in existing_memberships:
            if membership.custom_field.name not in expected_memberships:
                raise ValueError(
                    f"Core fieldset unexpected membership: {namespace}/{slug}#{membership.custom_field.name}"
                )
        fieldset.field_memberships.all().delete()
        CustomFieldsetField.objects.bulk_create(
            [
                CustomFieldsetField(
                    fieldset=fieldset,
                    custom_field=custom_fields[member["field"]],
                    position=member["position"],
                )
                for member in section["memberships"]
            ]
        )
        fieldsets[slug] = fieldset
    return fieldsets


def _seed_category_defaults(category_rows, categories, fieldsets, *, preserve_matching_existing=False):
    for category_row in category_rows:
        category = categories[category_row["slug"]]
        existing_memberships = list(
            category.default_fieldset_memberships.select_related("fieldset").order_by("position")
        )
        if any(
            membership.fieldset.management_kind != CustomFieldset.MANAGEMENT_CORE for membership in existing_memberships
        ):
            raise ValueError(f"Core category default ownership collision: {category_row['slug']}")
        if preserve_matching_existing and existing_memberships:
            expected_memberships = [
                (fieldsets[item["fieldset"].rsplit("/", 1)[1]].pk, item["position"])
                for item in category_row["default_fieldsets"]
            ]
            actual_memberships = [(membership.fieldset_id, membership.position) for membership in existing_memberships]
            if actual_memberships != expected_memberships:
                raise ValueError(f"Local demo category default composition collision: {category_row['slug']}")
            continue
        category.default_fieldset_memberships.all().delete()
        CategoryDefaultFieldset.objects.bulk_create(
            [
                CategoryDefaultFieldset(
                    category=category,
                    fieldset=fieldsets[item["fieldset"].rsplit("/", 1)[1]],
                    position=item["position"],
                )
                for item in category_row["default_fieldsets"]
            ]
        )


def _demo_category_defaults(category):
    defaults = list(category.default_fieldset_memberships.select_related("fieldset").order_by("position"))
    if not defaults:
        raise ValueError(f"Demo Asset Type category has no default fieldsets: {category.slug}")
    return defaults


def _demo_operating_system_family(atype_slug):
    if "macbook" in atype_slug or "mac-studio" in atype_slug:
        return "macos"
    if "iphone" in atype_slug:
        return "ios"
    if "ipad" in atype_slug:
        return "ipados"
    if "galaxy" in atype_slug:
        return "android"
    if atype_slug == "synology-ds1823xs":
        return "embedded"
    if atype_slug == "logitech-rally-bar":
        return "embedded"
    if atype_slug in ("dell-poweredge-r760", "hpe-proliant-dl380-g11"):
        return "linux"
    if atype_slug in ("cisco-catalyst-9300", "unifi-switch-pro-48", "meraki-mr46", "unifi-dream-machine-pro"):
        return "network_os"
    return "windows"


def _add_default_demo_specs(specs, atype_slug, category_slug):
    specs.setdefault(
        "form_factor",
        {
            "laptops": "notebook",
            "desktops": "desktop",
            "servers": "rack",
            "storage-devices": "appliance",
            "switches": "appliance",
            "routers": "appliance",
            "access-points": "appliance",
            "mobile-phones": "phone",
            "tablets": "tablet",
            "monitors": "peripheral",
            "conference-systems": "appliance",
        }.get(category_slug, "other"),
    )
    if category_slug != "monitors":
        specs.setdefault("operating_system_family", _demo_operating_system_family(atype_slug))
    if category_slug in {"laptops", "desktops", "servers", "storage-devices"}:
        specs.setdefault("memory_type", "ddr5")
        specs.setdefault("ethernet_port_count", 1)
        specs.setdefault("ethernet_speeds", ["1g"])
        specs.setdefault("usb_port_count", 4)
    elif category_slug in {"mobile-phones", "tablets"}:
        specs.setdefault("memory_type", "lpddr5")
        specs.setdefault("ethernet_port_count", 0)
        specs.setdefault("ethernet_speeds", [])
        specs.setdefault("usb_port_count", 1)
    elif category_slug in {"switches", "routers", "access-points"}:
        specs.setdefault("ethernet_speeds", ["1g"] if atype_slug == "meraki-mr46" else ["10g", "1g"])
        specs.setdefault("usb_port_count", 1)
        specs.setdefault(
            "network_functions",
            {
                "cisco-catalyst-9300": ["switch"],
                "unifi-switch-pro-48": ["switch"],
                "meraki-mr46": ["wlan_ap"],
                "unifi-dream-machine-pro": ["firewall", "router"],
            }[atype_slug],
        )
        if atype_slug == "meraki-mr46":
            specs.setdefault("ethernet_port_count", 1)
            specs.setdefault("wifi_standards", ["802_11ac", "802_11ax"])
    if "wifi_standards" not in specs and category_slug in {
        "laptops",
        "desktops",
        "mobile-phones",
        "tablets",
    }:
        specs["wifi_standards"] = ["802_11ac", "802_11ax"]
    if "management_protocols" not in specs and category_slug in {
        "laptops",
        "desktops",
        "servers",
        "storage-devices",
        "switches",
        "routers",
        "access-points",
    }:
        specs["management_protocols"] = sorted(["https", "ssh"])
    return specs


class SeedCatalogMixin:
    """Mixin for Command(BaseCommand).  Reads/writes self._ registries."""

    def _seed_catalog(self):
        # inline imports: app-registry: Resolve seed models only after Django has populated the app registry.
        from assets.models import AssetRole, AssetType, Category, Depreciation, Manufacturer, StatusLabel, Supplier
        from inventory.models import Component
        from software.models import Software

        self.stdout.write("--- Catalog: reference data ---")

        # Status labels
        self._status_labels = {}
        for name, slug, stype, color in self._status_label_defs():
            obj, _ = StatusLabel.objects.get_or_create(
                slug=slug, defaults={"name": name, "type": stype, "color": color}
            )
            self._status_labels[slug] = obj

        # Tags
        self._tags = {}
        for name, slug, color in [
            ("Production", "production", "28a745"),
            ("Development", "development", "007bff"),
            ("VIP", "vip", "dc3545"),
            ("GxP Validated", "gxp-validated", "198754"),
            ("PCI Scope", "pci-scope", "0b5ed7"),
            ("Finance", "finance", "198754"),
            ("Field", "field", "fd7e14"),
            ("Loaner", "loaner", "adb5bd"),
            ("Critical", "critical", "dc3545"),
            ("Legacy", "legacy", "6c757d"),
            ("Encrypted", "encrypted", "20c997"),
            ("MDM Enrolled", "mdm-enrolled", "6f42c1"),
        ]:
            obj, _ = Tag.objects.get_or_create(slug=slug, defaults={"name": name, "color": color})
            self._tags[slug] = obj

        # Asset roles — (name, slug, color, desc, allows_components)
        self._asset_roles = {}
        for name, slug, color, desc, allows_comp in [
            ("Standard Workstation", "standard-workstation", "007bff", "Laptop/desktop for general office staff", True),
            (
                "Developer Workstation",
                "developer-workstation",
                "6f42c1",
                "High-performance workstation for engineers",
                True,
            ),
            ("Executive Workstation", "executive-workstation", "e83e8c", "Premium device for executives", True),
            ("CAD/Design Workstation", "cad-design-workstation", "fd7e14", "GPU workstation for CAD/3D", True),
            (
                "Lab / Cleanroom Terminal",
                "lab-terminal",
                "adb5bd",
                "Restricted terminal for lab or production-floor use",
                False,
            ),
            ("Field Tablet", "field-tablet", "20c997", "Ruggedized tablet for field/warehouse work", False),
            ("Corporate Smartphone", "corporate-smartphone", "fd7e14", "Company smartphone for voice/chat/MFA", False),
            (
                "Virtualization Host",
                "virtualization-host-server",
                "dc3545",
                "Hypervisor host (ESXi/Proxmox/Hyper-V)",
                True,
            ),
            ("Database Server", "database-server", "17a2b8", "Production database host", True),
            ("Application Server", "application-server", "20c997", "Line-of-business application host", True),
            ("Backup / Storage", "backup-server", "fd7e14", "Backup target or NAS", True),
            ("Core Router / Firewall", "core-router-firewall", "dc3545", "Edge security gateway", False),
            ("Access / Distribution Switch", "access-switch", "0d6efd", "Network switch", False),
            ("Wireless Access Point", "wireless-ap", "20c997", "Enterprise WiFi access point", False),
            ("Conference Room AV", "conference-av", "e83e8c", "Meeting-room camera/audio hub", False),
            ("Desktop Monitor", "desktop-monitor", "6f42c1", "External display", False),
        ]:
            obj, _ = AssetRole.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "color": color, "description": desc, "allows_components": allows_comp},
            )
            self._asset_roles[slug] = obj

        # Manufacturers
        self._manufacturers = {}
        for name, slug in [
            ("Dell Technologies", "dell-technologies"),
            ("Apple Inc.", "apple-inc"),
            ("HP Inc.", "hp-inc"),
            ("Lenovo Group", "lenovo-group"),
            ("Cisco Systems", "cisco-systems"),
            ("Samsung Electronics", "samsung-electronics"),
            ("Microsoft Corporation", "microsoft-corporation"),
            ("Logitech International", "logitech-international"),
            ("Brother Industries", "brother-industries"),
            ("Synology Inc.", "synology-inc"),
            ("Ubiquiti Inc.", "ubiquiti-inc"),
        ]:
            obj, _ = Manufacturer.objects.get_or_create(slug=slug, defaults={"name": name})
            self._manufacturers[slug] = obj

        # Suppliers
        from django.contrib.contenttypes.models import ContentType as CT

        from organization.models import Contact, ContactAssignment, ContactRole

        supplier_ct = CT.objects.get_for_model(Supplier)
        primary_role, _ = ContactRole.objects.get_or_create(
            slug="primary-contact",
            defaults={"name": "Primary Contact", "description": "Primary Contact"},
        )
        self._suppliers = {}
        for name, slug, email, phone, website in [
            (
                "Northwind Procurement",
                "northwind-procurement",
                "buy@northwind-it.com",
                "+49-30-555-0100",
                "https://northwind-it.com",
            ),
            ("Dell Direct", "dell-direct", "enterprise@dell.com", "+1-800-555-0199", "https://dell.com"),
            ("Apple Business", "apple-business", "business@apple.com", "+1-800-555-0200", "https://apple.com/business"),
            ("CDW Deutschland", "cdw-deutschland", "de.sales@cdw.com", "+49-211-555-0500", "https://cdw.de"),
            ("Bechtle AG", "bechtle-ag", "b2b@bechtle.com", "+49-7132-555-0700", "https://bechtle.com"),
            ("Insight Enterprises", "insight-enterprises", "eu@insight.com", "+44-20-555-0800", "https://insight.com"),
        ]:
            obj, created = Supplier.objects.get_or_create(slug=slug, defaults={"name": name, "website": website})
            if created and not obj.contacts.filter(priority="primary").exists():
                contact = Contact.objects.create(
                    name=f"{name} Contact",
                    phone=phone,
                    email=email,
                )
                ContactAssignment.objects.create(
                    contact=contact,
                    role=primary_role,
                    content_type=supplier_ct,
                    object_id=obj.pk,
                    priority="primary",
                )
            self._suppliers[slug] = obj

        # Depreciation schedules — generic named first (used by asset types)
        self._depreciations = {}
        for name, months in [
            ("3-Year Straight-Line", 36),
            ("4-Year Straight-Line", 48),
            ("5-Year Straight-Line", 60),
            ("7-Year Straight-Line", 84),
        ]:
            obj, _ = Depreciation.objects.get_or_create(name=name, defaults={"months": months})
            self._depreciations[name] = obj

        # German AfA / GWG example policies for the demo tenant (opt-in via seed_data;
        # migration 0043 also seeds these on every install — see FIX-05 note).
        _afa_policies = [
            {
                "name": "IT-Hardware 36 Monate (AfA)",
                "months": 36,
                "method": "straight_line",
                "convention": "include_purchase_month",
                "description": "AfA-Tabelle 2021: Computer, Notebooks, Tablets (3 Jahre)",
            },
            {
                "name": "Server 60 Monate (AfA)",
                "months": 60,
                "method": "straight_line",
                "convention": "include_purchase_month",
                "description": "AfA-Tabelle 2021: Server und Workstations (5 Jahre)",
            },
            {
                "name": "Sofortabschreibung GWG (≤ 800 €)",
                "months": 1,
                "method": "straight_line",
                "convention": "include_purchase_month",
                "immediate_expense_threshold": "800.00",
                "description": "Geringwertige Wirtschaftsgüter nach § 6 Abs. 2 EStG: Sofortabschreibung bis 800 €",
            },
        ]
        self._demo_depreciation_afa = None
        for p in _afa_policies:
            obj, _ = Depreciation.objects.get_or_create(
                name=p["name"],
                defaults={k: v for k, v in p.items() if k != "name"},
            )
            if self._demo_depreciation_afa is None:
                self._demo_depreciation_afa = obj  # first entry = tenant default showcase

        # Normative core vocabulary is the runtime consumer of the current release.
        from django.contrib.contenttypes.models import ContentType

        from assets.models import Asset as AssetModel
        from assets.models import AssetType as AssetTypeModel

        # inline import: app-registry: load the assets-owned release after Django setup.
        from assets.services.specifications.core_vocabulary import get_core_vocabulary

        vocabulary = get_core_vocabulary()
        vocabulary_version = vocabulary["version"]
        asset_ct = ContentType.objects.get_for_model(AssetModel)
        assettype_ct = ContentType.objects.get_for_model(AssetTypeModel)

        self._choice_sets = _reconcile_core_choice_set(vocabulary["choice_sets"])
        core_field_rows = vocabulary["active_fields"] + vocabulary["reserved_retired_fields"]
        self._custom_fields = _reconcile_core_fields(
            core_field_rows,
            self._choice_sets,
            asset_ct,
            assettype_ct,
            vocabulary_version,
        )
        self._fieldsets = _reconcile_core_fieldsets(vocabulary["sections"], self._custom_fields, vocabulary_version)
        seed_core_vocabulary_translations(apps.get_model, vocabulary=vocabulary)

        canonical_category_rows = vocabulary["categories"]

        # Local catalog categories remain available to users. Seeded network
        # models use the specific starter categories instead of network-devices.
        self._categories = {}
        category_defs = [
            ("laptops", "4263eb"),
            ("desktops", "1864ab"),
            ("servers", "5f3dc4"),
            ("monitors", "0c8599"),
            ("mobile-phones", "2b8a3e"),
            ("tablets", "37b24d"),
            ("network-devices", "e8590c"),
            ("storage-devices", "9c36b5"),
            ("conference-systems", "1098ad"),
            ("charger", "f59f00"),
            ("adaptor", "f08c00"),
            ("mouse", "868e96"),
            ("keyboard", "495057"),
            ("webcam", "0ca678"),
            ("headset", "7048e8"),
            ("cable", "adb5bd"),
            ("display", "15aabf"),
            ("dock", "3b5bdb"),
            ("toner", "343a40"),
            ("ink", "1c7ed6"),
            ("batteries", "66a80f"),
            ("thermal-paste", "c2255c"),
            ("other", "6c757d"),
            ("ram-memory", "e64980"),
            ("ssd-nvme", "be4bdb"),
            ("hdd", "7950f2"),
            ("nic", "f76707"),
            ("gpu", "e03131"),
            ("cpu", "d6336c"),
        ]
        applies = {"asset": True, "accessory": True, "consumable": True, "component": True}
        for slug, color in category_defs:
            obj, created = Category.objects.get_or_create(
                slug=slug,
                defaults={"name": slug.replace("-", " ").title(), "applies_to": applies, "color": color},
            )
            if not created and not obj.color:
                # Backfill a category seeded before colours were assigned.
                obj.color = color
                obj.save(update_fields=["color"])
            self._categories[slug] = obj

        for category_row in canonical_category_rows:
            obj, created = Category.objects.get_or_create(
                slug=category_row["slug"],
                defaults={
                    "name": category_row["label"],
                    "description": category_row["description"],
                    "applies_to": {target: True for target in category_row["applies_to"]},
                },
            )
            if not created:
                obj.name = category_row["label"]
                obj.description = category_row["description"]
                obj.applies_to = {target: True for target in category_row["applies_to"]}
                obj.save(update_fields=["name", "description", "applies_to"])
            self._categories[category_row["slug"]] = obj

        _seed_category_defaults(canonical_category_rows, self._categories, self._fieldsets)

        # The starter taxonomy has no NAS or conference AV categories, so those
        # two demo examples keep explicit local categories and compositions.
        local_demo_category_rows = [
            {
                "slug": "storage-devices",
                "default_fieldsets": [
                    {"fieldset": f"itambox/{slug}", "position": 10 * order}
                    for order, slug in enumerate(
                        (
                            "product-physical",
                            "compute-memory",
                            "storage",
                            "connectivity-io",
                            "power-battery",
                            "management-security",
                            "compliance-sustainability",
                        ),
                        start=1,
                    )
                ],
            },
            {
                "slug": "conference-systems",
                "default_fieldsets": [
                    {"fieldset": f"itambox/{slug}", "position": 10 * order}
                    for order, slug in enumerate(
                        (
                            "product-physical",
                            "connectivity-io",
                            "power-battery",
                            "display-av-imaging",
                            "management-security",
                            "compliance-sustainability",
                        ),
                        start=1,
                    )
                ],
            },
        ]
        _seed_category_defaults(
            local_demo_category_rows,
            self._categories,
            self._fieldsets,
            preserve_matching_existing=True,
        )

        # Asset types: (model, slug, manufacturer, part, eol, depreciation, category, role, specifications)
        at_data = [
            (
                "Latitude 5550",
                "dell-latitude-5550",
                "dell-technologies",
                "LAT5550-2025",
                36,
                "3-Year Straight-Line",
                "laptops",
                "standard-workstation",
                {
                    "processor_model": "Intel Core i7-1365U",
                    "memory_capacity": "16.000",
                    "storage_capacity": "512.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "EliteBook 860 G11",
                "hp-elitebook-860-g11",
                "hp-inc",
                "866S7EA",
                36,
                "3-Year Straight-Line",
                "laptops",
                "standard-workstation",
                {
                    "processor_model": "Intel Core i7-1370P",
                    "memory_capacity": "32.000",
                    "storage_capacity": "1024.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "ThinkPad X1 Carbon Gen 12",
                "thinkpad-x1-carbon-g12",
                "lenovo-group",
                "21KC004PGE",
                36,
                "3-Year Straight-Line",
                "laptops",
                "developer-workstation",
                {
                    "processor_model": "Intel Core i7-1365U",
                    "memory_capacity": "32.000",
                    "storage_capacity": "1024.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                'MacBook Pro 16"',
                "macbook-pro-16-2024",
                "apple-inc",
                "MBP16-M4",
                36,
                "3-Year Straight-Line",
                "laptops",
                "developer-workstation",
                {
                    "processor_model": "Apple M4 Pro",
                    "memory_capacity": "36.000",
                    "storage_capacity": "1024.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                'MacBook Air 15"',
                "macbook-air-15-2024",
                "apple-inc",
                "MBA15-M3",
                36,
                "3-Year Straight-Line",
                "laptops",
                "standard-workstation",
                {
                    "processor_model": "Apple M3",
                    "memory_capacity": "16.000",
                    "storage_capacity": "512.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "Precision 5680",
                "dell-precision-5680",
                "dell-technologies",
                "PREC5680-WS",
                48,
                "4-Year Straight-Line",
                "laptops",
                "developer-workstation",
                {
                    "processor_model": "Intel Core i9-13900H",
                    "memory_capacity": "64.000",
                    "storage_capacity": "2048.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "OptiPlex 7010 SFF",
                "dell-optiplex-7010",
                "dell-technologies",
                "OPT7010-SFF",
                48,
                "4-Year Straight-Line",
                "desktops",
                "standard-workstation",
                {
                    "processor_model": "Intel Core i5-13500",
                    "memory_capacity": "16.000",
                    "storage_capacity": "512.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "Mac Studio",
                "mac-studio-2024",
                "apple-inc",
                "MSTUDIO-M2U",
                60,
                "5-Year Straight-Line",
                "desktops",
                "cad-design-workstation",
                {
                    "processor_model": "Apple M2 Ultra",
                    "memory_capacity": "64.000",
                    "storage_capacity": "1024.000",
                    "storage_medium": "ssd",
                    "storage_interface": "nvme",
                },
            ),
            (
                "Precision 7960 Tower",
                "dell-precision-7960-tower",
                "dell-technologies",
                "PREC7960-TWR",
                60,
                "5-Year Straight-Line",
                "desktops",
                "cad-design-workstation",
                {
                    "processor_model": "Intel Xeon w7-3465X",
                    "memory_capacity": "128.000",
                    "storage_capacity": "4096.000",
                    "storage_medium": "ssd",
                },
            ),
            (
                "PowerEdge R760",
                "dell-poweredge-r760",
                "dell-technologies",
                "R760-XEON",
                60,
                "5-Year Straight-Line",
                "servers",
                "virtualization-host-server",
                {
                    "processor_model": "2x Intel Xeon Gold 6430",
                    "memory_capacity": "256.000",
                    "storage_capacity": "8000.000",
                    "storage_medium": "ssd",
                },
            ),
            (
                "ProLiant DL380 Gen11",
                "hpe-proliant-dl380-g11",
                "hp-inc",
                "P52534-B21",
                60,
                "5-Year Straight-Line",
                "servers",
                "application-server",
                {
                    "processor_model": "2x Intel Xeon Silver 4416+",
                    "memory_capacity": "128.000",
                    "storage_capacity": "4000.000",
                    "storage_medium": "ssd",
                },
            ),
            (
                "DiskStation DS1823xs+",
                "synology-ds1823xs",
                "synology-inc",
                "DS1823XS+",
                60,
                "5-Year Straight-Line",
                "storage-devices",
                "backup-server",
                {
                    "processor_model": "AMD Ryzen V1780B",
                    "memory_capacity": "32.000",
                    "memory_type": "ddr4",
                    "storage_capacity": "64000.000",
                    "storage_medium": "hdd",
                },
            ),
            (
                "iPhone 15 Pro",
                "iphone-15-pro",
                "apple-inc",
                "A2847",
                24,
                "3-Year Straight-Line",
                "mobile-phones",
                "corporate-smartphone",
                {
                    "processor_model": "Apple A17 Pro",
                    "memory_capacity": "8.000",
                    "storage_capacity": "256.000",
                    "display_size": "6.10",
                },
            ),
            (
                "Galaxy S24 Ultra",
                "galaxy-s24-ultra",
                "samsung-electronics",
                "SM-S928B",
                24,
                "3-Year Straight-Line",
                "mobile-phones",
                "corporate-smartphone",
                {
                    "processor_model": "Snapdragon 8 Gen 3",
                    "memory_capacity": "12.000",
                    "storage_capacity": "256.000",
                    "display_size": "6.80",
                },
            ),
            (
                'iPad Pro 12.9"',
                "ipad-pro-129-2024",
                "apple-inc",
                "A2436",
                36,
                "3-Year Straight-Line",
                "tablets",
                "field-tablet",
                {
                    "processor_model": "Apple M4",
                    "memory_capacity": "8.000",
                    "storage_capacity": "256.000",
                    "display_size": "12.90",
                },
            ),
            (
                "Surface Pro 10",
                "surface-pro-10",
                "microsoft-corporation",
                "SURFPRO10-I7",
                36,
                "3-Year Straight-Line",
                "tablets",
                "field-tablet",
                {
                    "processor_model": "Intel Core i7-1365U",
                    "memory_capacity": "16.000",
                    "storage_capacity": "512.000",
                    "display_size": "13.00",
                },
            ),
            (
                "Catalyst 9300",
                "cisco-catalyst-9300",
                "cisco-systems",
                "C9300-48P",
                84,
                "7-Year Straight-Line",
                "switches",
                "access-switch",
                {"ethernet_port_count": 48, "poe_budget": "740.000"},
            ),
            (
                "UniFi Switch Pro 48 PoE",
                "unifi-switch-pro-48",
                "ubiquiti-inc",
                "USW-PRO-48-POE",
                60,
                "5-Year Straight-Line",
                "switches",
                "access-switch",
                {"ethernet_port_count": 48, "poe_budget": "600.000"},
            ),
            (
                "Meraki MR46",
                "meraki-mr46",
                "cisco-systems",
                "MR46-HW",
                60,
                "5-Year Straight-Line",
                "access-points",
                "wireless-ap",
                {},
            ),
            (
                "UniFi Dream Machine Pro",
                "unifi-dream-machine-pro",
                "ubiquiti-inc",
                "UDM-Pro",
                60,
                "5-Year Straight-Line",
                "routers",
                "core-router-firewall",
                {"ethernet_port_count": 8, "poe_budget": "0.000"},
            ),
            (
                'Dell P2723DE 27" Monitor',
                "dell-p2723de-monitor",
                "dell-technologies",
                "P2723DE",
                60,
                "5-Year Straight-Line",
                "monitors",
                "desktop-monitor",
                {},
            ),
            (
                'Dell P2422HE 24" Monitor',
                "dell-p2422he-monitor",
                "dell-technologies",
                "P2422HE",
                60,
                "5-Year Straight-Line",
                "monitors",
                "desktop-monitor",
                {},
            ),
            (
                "Logitech Rally Bar",
                "logitech-rally-bar",
                "logitech-international",
                "960-001308",
                60,
                "5-Year Straight-Line",
                "conference-systems",
                "conference-av",
                {},
            ),
        ]

        self._asset_types = {}
        for model_name, slug, mfr, part, eol, dep, cat, role, specifications in at_data:
            specs = _add_default_demo_specs(dict(specifications), slug, cat)
            obj, _ = AssetType.objects.get_or_create(
                slug=slug,
                defaults={
                    "model": model_name,
                    "manufacturer": self._manufacturers[mfr],
                    "part_number": part,
                    "eol_months": eol,
                    "depreciation": self._depreciations[dep],
                    "category": self._categories[cat],
                    "asset_role": self._asset_roles[role],
                    "custom_field_data": {},
                    "management_kind": AssetType.MANAGEMENT_LOCAL,
                    "region": "",
                    "configuration": "",
                },
            )
            obj.model = model_name
            obj.manufacturer = self._manufacturers[mfr]
            obj.part_number = part
            obj.eol_months = eol
            obj.depreciation = self._depreciations[dep]
            obj.category = self._categories[cat]
            obj.asset_role = self._asset_roles[role]
            obj.management_kind = AssetType.MANAGEMENT_LOCAL
            obj.region = ""
            obj.configuration = ""
            obj.save(
                update_fields=[
                    "model",
                    "manufacturer",
                    "part_number",
                    "eol_months",
                    "depreciation",
                    "category",
                    "asset_role",
                    "management_kind",
                    "region",
                    "configuration",
                ]
            )
            obj.fieldset_memberships.all().delete()
            category_defaults = _demo_category_defaults(self._categories[cat])
            AssetTypeFieldset.objects.bulk_create(
                [
                    AssetTypeFieldset(asset_type=obj, fieldset=membership.fieldset, position=membership.position)
                    for membership in category_defaults
                ]
            )
            write_custom_field_data(obj, specs, update_fields=("custom_field_data",))
            self._asset_types[slug] = obj

        # Components
        self._components = {}
        for name, slug, mfr, cat, part, specs in [
            (
                "Samsung 32GB DDR5-4800",
                "samsung-32gb-ddr5",
                "samsung-electronics",
                "ram-memory",
                "M324R4GA3BB0",
                {"capacity_gb": 32, "type": "DDR5"},
            ),
            (
                "Crucial 16GB DDR5-5600",
                "crucial-16gb-ddr5",
                "samsung-electronics",
                "ram-memory",
                "CT16G56C46S5",
                {"capacity_gb": 16, "type": "DDR5"},
            ),
            (
                "Samsung 1TB 990 Pro NVMe",
                "samsung-1tb-nvme",
                "samsung-electronics",
                "ssd-nvme",
                "MZ-V9P1T0B",
                {"capacity_gb": 1000, "type": "NVMe"},
            ),
            (
                "Samsung 2TB 990 Pro NVMe",
                "samsung-2tb-nvme",
                "samsung-electronics",
                "ssd-nvme",
                "MZ-V9P2T0B",
                {"capacity_gb": 2000, "type": "NVMe"},
            ),
            (
                "WD Red Pro 8TB HDD",
                "wd-red-8tb",
                "dell-technologies",
                "hdd",
                "WD8003FFBX",
                {"capacity_gb": 8000, "type": "HDD"},
            ),
            (
                "Seagate IronWolf Pro 12TB",
                "seagate-ironwolf-12tb",
                "dell-technologies",
                "hdd",
                "ST12000NE0008",
                {"capacity_gb": 12000, "type": "HDD"},
            ),
            ("Intel X710 10GbE NIC", "intel-x710-nic", "dell-technologies", "nic", "X710DA2", {"speed": "10GbE"}),
            ("NVIDIA RTX 6000 Ada 48GB", "nvidia-rtx-6000", "dell-technologies", "gpu", "RTX6000-ADA", {"vram_gb": 48}),
            ("Intel Xeon Gold 6430", "xeon-gold-6430", "dell-technologies", "cpu", "SRMZS", {"cores": 32}),
            (
                "Dell PERC H755 RAID Controller",
                "dell-perc-h755",
                "dell-technologies",
                "other",
                "PERC-H755",
                {"interface": "SAS 12Gb/s"},
            ),
        ]:
            obj, _ = Component.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "manufacturer": self._manufacturers[mfr],
                    "category": self._categories[cat],
                    "part_number": part,
                    "specs": specs,
                },
            )
            self._components[slug] = obj

        # Accessories (tenant set later per stock; catalog rows are global definitions)
        self._accessory_defs = [
            ("USB-C Charger 65W", "usb-c-charger-65w", "dell-technologies", "charger", "450-AFGM", 10),
            ("USB-C to HDMI Adapter", "usb-c-hdmi-adapter", "dell-technologies", "adaptor", "470-AEGM", 10),
            ("Wireless Mouse MX Master 3S", "mx-master-3s", "logitech-international", "mouse", "910-006556", 10),
            ("Wireless Keyboard MX Keys", "mx-keys", "logitech-international", "keyboard", "920-009413", 10),
            ("Webcam Brio 500", "webcam-brio-500", "logitech-international", "webcam", "960-001422", 8),
            ("Headset Zone Wireless 2", "zone-wireless-2", "logitech-international", "headset", "981-000886", 8),
            ("Thunderbolt 4 Dock", "tb4-dock", "dell-technologies", "dock", "WD22TB4", 6),
            ('Dell 27" Monitor P2723DE', "dell-p2723de", "dell-technologies", "display", "DELL-P2723DE", 6),
        ]
        self._consumable_defs = [
            ("HP 26X Laser Toner - Black", "hp-26x-toner-black", "hp-inc", "toner", "CF226X", 5),
            ("Brother DR-241CL Drum Unit", "brother-dr-241cl", "brother-industries", "toner", "DR-241CL", 3),
            ("Arctic MX-6 Thermal Paste", "arctic-mx-6", "dell-technologies", "thermal-paste", "MX6-4G", 8),
            ("AA Batteries Pack 24", "aa-batteries-24", "logitech-international", "batteries", "AA-24PK", 15),
        ]
        # Accessory/Consumable catalogue objects are created per primary tenant
        # (they are tenant-scoped); store the definitions for the stock phase.
        self._accessories = {}
        self._consumables = {}

        # Software
        self._software = {}
        for name, mfr in [
            ("Windows 11 Enterprise", "microsoft-corporation"),
            ("macOS Sequoia", "apple-inc"),
            ("Microsoft 365 E5", "microsoft-corporation"),
            ("Microsoft Office LTSC 2024", "microsoft-corporation"),
            ("Adobe Creative Cloud", "microsoft-corporation"),
            ("JetBrains All Products Pack", "microsoft-corporation"),
            ("VMware vSphere 8 Enterprise Plus", "dell-technologies"),
            ("CrowdStrike Falcon", "microsoft-corporation"),
            ("1Password Business", "microsoft-corporation"),
            ("Zoom Workplace Enterprise", "microsoft-corporation"),
            ("Veeam Backup & Replication", "dell-technologies"),
            ("Autodesk AutoCAD", "microsoft-corporation"),
            ("SAS Analytics Pro", "dell-technologies"),
            ("Bloomberg Terminal", "microsoft-corporation"),
            ("Ubuntu Pro 24.04", "dell-technologies"),
        ]:
            obj, _ = Software.objects.get_or_create(name=name, defaults={"manufacturer": self._manufacturers[mfr]})
            self._software[name] = obj

        # SaaS suppliers are created after tenants and tenant groups are seeded.
        self._saas_supplier_data = [
            ("Amazon Web Services", "aws-org", "https://console.aws.amazon.com"),
            ("Microsoft Azure", "azure-ea", "https://portal.azure.com"),
            ("Google Cloud Platform", "gcp-org", "https://console.cloud.google.com"),
            ("GitHub Enterprise", "github-ent", "https://github.com/enterprises"),
            ("Cloudflare", "cloudflare", "https://dash.cloudflare.com"),
            ("Datadog", "datadog", "https://app.datadoghq.eu"),
        ]
        self._saas_suppliers = {}

        self.stdout.write(
            f"  {len(self._asset_types)} asset types, {len(self._components)} components, "
            f"{len(self._software)} software products, {len(self._saas_supplier_data)} SaaS suppliers."
        )
