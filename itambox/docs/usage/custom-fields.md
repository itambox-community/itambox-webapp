# Custom Fields

Custom fields allow you to extend ITAMbox models with user-defined attributes
without modifying the database schema or source code. They capture organisation-specific data, from per-device
SIM card numbers and warranty tiers to department-specific approval flags.

Asset Types and Assets use **specifications**: an explicitly selected, ordered
composition of Fieldsets. A Field's target says where it is applicable; it does
not automatically add that Field or its Fieldset to every Asset Type.

Unlike hard-coded model fields which require a developer, custom fields are
created and managed entirely through the UI by administrators.

---

## Creating Custom Fields

Navigate to **Extras → Custom Fields** and click **Add**. Every custom field
has a core set of attributes:

| Attribute | Description |
|-----------|-------------|
| **Field Name** | Database-safe slug identifier (e.g. `sim_card_number`). Lowercase, no spaces. Used in templates and API queries. |
| **Display Label** | Human-friendly label shown in forms, list headers, and filters. |
| **Field Type** | Data type the field stores. Controls input widget and validation. |
| **Choice Set** | Relational set of ordered allowed choices for Single Select and Multi Select fields. Choice labels are managed separately from stable keys. |
| **Required** | Requires a present, type-valid value when the Field is active in the specification being written. It does not activate the Field or make every native-only edit a specification write. |

### Field Types

| Type | Slug | Input Widget | Example Use |
|------|------|-------------|-------------|
| Text | `text` | Single-line text input | Asset tag aliases, notes |
| Integer | `integer` | Number input | CPU core count, rack unit position |
| Decimal | `decimal` | Number input with configured scale | Power draw, capacity, temperature |
| Date | `date` | Date picker | Warranty end, inspection date |
| Boolean | `boolean` | Checkbox / Yes-No selector | "Under support contract", "Hazardous material" |
| Single Select | `single-select` | Dropdown | Tier level, department, site code |
| Multi Select | `multi-select` | Multi-select dropdown | Supported protocols, capabilities |

### Configuring Choices

When the field type is **Single Select** or **Multi Select**, select a
**Choice Set** containing the allowed choices. Choice keys are stable machine
identifiers; labels may be changed, but a key must never be reused for a new
meaning. Removed choices remain as deprecated tombstones so existing stored
values cannot silently change meaning.

The first active choice is not treated as a default — forms render an empty
option for Single Select unless the field is marked **Required**.

> [!IMPORTANT]
> Choice labels are presentation data; changing a label does not change the
> stored key. Removing a choice deprecates its key and preserves existing JSON
> values. The key remains reserved and cannot be assigned to a later unrelated
> choice.

---

## Binding to Object Types

Custom fields must be associated with one or more **object types**
(ContentTypes — the Django model classes in ITAMbox). This describes where a Field may apply. For Asset Types and Assets,
applicability and activation are separate: the Type's selected Fieldsets determine
which composed specifications are active.

### Supported Models

| Model | Content Type | What the field represents |
|-------|-------------|--------------------------|
| **Asset** | `assets.asset` | Per-device attributes (SIM number, desk location, cost centre) |
| **Asset Type** | `assets.assettype` | Catalogue/model specifications (form factor, TDP, PoE support); not copied into per-device values |
| **Tenant** | `organization.tenant` | Tenant-level configuration (region, billing code) |
| **Location** | `organization.location` | Site-level attributes (floor, building access code) |
| **Component** | `inventory.component` | Per-component specs (firmware version, slot position) |
| **Accessory** | `inventory.accessory` | Per-accessory attributes (adapter type, cable length) |
| **Consumable** | `inventory.consumable` | Per-consumable attributes (batch number, hazard class) |
| **License** | `licenses.license` | Per-license metadata (activation key, concurrent seat limit) |
| **Subscription** | `subscriptions.subscription` | Per-subscription attributes (renewal contact, PO number) |
| **Contract** | `procurement.contract` | Per-contract attributes (vendor ref, payment terms) |

> [!TIP]
> Binding a field to **Asset Type** creates a **hardware specification** —
> a model-level property (e.g. "RAM Slots: 4"). That value is stored on the Type,
> not inherited as an observed value by its Assets.
> Binding to **Asset** creates a **per-device detail** (e.g. "Installed RAM
> modules: SK-Hynix 16GB"). Choose the binding that matches what you are
> describing.

You can bind a single custom field to multiple object types. For example, a
"Cost Centre" field could apply to both Assets and Subscriptions.

---

## Required vs Optional

The **Required** checkbox on each custom field controls server-side validation:

| Setting | Behaviour |
|---------|-----------|
| **Required** (checked) | An active Field must contain a present, type-valid value when its specification is written or a Type/composition change activates it. Empty text (`""`), an empty multi-select (`[]`), an empty single-select, and `null` do not satisfy Required. Numeric zero and Boolean `false` are valid values. A required Boolean uses an explicit Yes/No input. |
| **Optional** (unchecked) | The field can be left blank. |

> [!IMPORTANT]
> For Asset Types and Assets, specification validation belongs to the explicit
> specification write or Type/composition change. A native-only update does not
> replace specification values or silently fill newly required Fields.
> Only active Fields participate in current required-value checks. Values outside
> the active composition are retained as history, not made required merely by
> their target binding. Supported imports and API specification writes use the
> same validation rules.

---

## Custom Fieldsets

Custom Fieldsets group related Fields into ordered sections. For Asset Types
and Assets, select the intended Fieldsets on the Type; applicability alone is
not selection. Both the order of Fieldsets and the order of their members matter.

### Creating a Fieldset

Navigate to **Extras → Custom Fieldsets** and click **Add**:

| Attribute | Description |
|-----------|-------------|
| **Fieldset Name** | Section heading displayed in the form (e.g. "Network Configuration", "Financial Details") |
| **Custom Fields** | The fields to include in this group |

### Selecting an Asset Type composition

Add the Fieldsets that belong to the Type, in the intended order. A Fieldset
appears in that composition because it was selected, not because one of its
Fields targets Assets. Applicable active members form the specification. If a
Field occurs in more than one selected Fieldset, its first placement determines
where it appears; repeated membership does not create a second value.

When creating a Type, omitting the composition uses the selected Category's
defaults. Selecting an explicit empty composition means no Fieldsets; it does
not request defaults. The copied composition is independent of later changes to
Category defaults.

Removing a Fieldset or changing an Asset's Type does not erase observed values
that stop being active. Those values remain history. Model-level Type values
are not copied into the Asset. Review the proposed composition before saving;
if its definition or the object changed since preview, refresh and review the
new state instead of replaying stale revisions.

### Example

```
Fieldset: "Network Details"
  ├─ IPv4 Address (Text, bound to Asset)
  ├─ VLAN ID (Number, bound to Asset)
  └─ PoE Enabled (Boolean, bound to Asset Type)
```

If the Type explicitly includes this Fieldset, its Asset-targeted Fields
contribute to the Asset specification and its Type-targeted Fields contribute
to the Type specification. Merely creating the Fieldset activates neither.

---

## Custom Fields in the UI

### Detail Pages

On object detail pages (e.g. an Asset detail view), custom field values are
displayed in a **Custom Fields** card alongside the object's standard
attributes. Fields are grouped by their assigned fieldset; ungrouped fields
appear under a default heading.

### List Views

Custom fields that are bound to the listed model are available as **optional
columns** in the list view. Use the column selector to toggle them on/off
per list.

Custom fields also appear as **filter fields** in the list view filter panel.
Text fields support substring search; number fields support range operators;
select fields render as multi-select dropdowns; boolean fields as checkboxes;
date fields support date-range pickers.

### Exports

Custom field columns are available in the **export column selector** alongside
standard model fields. When you create an export (CSV, JSON, or via an
Export Template), custom field values are included.

### REST API

Asset and Asset Type specification reads use the read-only `specifications`
map, with stable Field keys, plus specification-state and revision metadata.
They do not accept the retired `custom_field_data` write alias. Other generic
custom-field models may still expose a read-only `custom_field_data` map.

For an Asset, a read-value example is:

```json
{
  "id": 42,
  "asset_tag": "IT-00042",
  "specifications": {
    "sim_card_number": "example-sim",
    "support_tier": "gold"
  }
}
```

Writes use the separate `specification_patch` envelope. `set` contains values
keyed by Field Name and `clear` contains the names to remove. This transport
contract is frozen for 1.0; composition and revision/history operations remain
separate from the shared custom-field value-write service:

```json
{
  "specification_patch": {
    "set": {"support_tier": "platinum"},
    "clear": ["sim_card_number"]
  }
}
```

The examples show the value maps only: Asset/Type writes must also supply the
required object/definition preconditions, and Asset operations require an
explicit authorized scope. Use Choice keys, not display labels.

`custom_field_data` is not a write target. Values that are no longer represented
by an active field definition remain readable and are preserved by ordinary
updates; they can only be changed through a valid explicit patch while their
field definition is writable. A field literally named `set` or `clear` is
addressed inside the `set`/`clear` operation, for example
`{"specification_patch": {"set": {"set": "value"}}}`.

---

For a composition change, obtain the prospective preconditions from
`POST /api/assets/asset-types/{id}/composition-preview/`, submitting the intended
`fieldsets` and `specification_patch`. Submit its `expected_resource_revision` and
`expected_definition_revision` with the composition write. A revision from the
current definition describes the old composition, not the proposed one.
The preview is read-only; the write still rechecks both submitted revisions.

## Shared Asset Type definitions

The in-app Type Library (library import, release history, snapshots and forks)
has been removed. Reusable Asset Type models are shared through the community
catalogue instead: one short YAML file per model, imported through the regular
bulk import ([epic #772](https://github.com/itambox-community/itambox-webapp/issues/772)).
The specification model described on this page is unchanged: managed core
fieldsets, quantity kinds and units, ordered specifications with history, and
the core vocabulary.

### Source-qualified specification consumers

Asset observations and model-level Asset Type values are separate sources.
Saved specification columns identify both source and stable Field key, for
example `asset.spec.support_tier` and `asset_type.spec.support_tier`.
Changing a display label does not change those identities.

The **Specification filters** field accepts an explicit filter document. For a
Field named `support_tier`, an Asset-value equality filter is:

```json
{"filters": [{"source": "asset", "field_key": "support_tier", "operator": "eq", "value": "gold"}]}
```

Use `asset_type` as the source to test the model-level value instead. Current
values are selected by default; missing, null, empty, historical, invalid, and
unknown states are not interchangeable. Reports and exports retain the selected
source and apply authorized Tenant scope before filtering. Saved label-based or
unresolved references are not silently redirected to another Field.

---

## Troubleshooting

**Custom field is not appearing on the edit form**
: The field's `object_types` binding does not match the model you are
  viewing. Check the field's configuration — it may be bound to `Asset Type`
  when you expected it on `Asset` (or vice versa).

**A specification change reports a missing required value**
: Check the proposed active composition and Field target. Supply a valid value
  for the activated Field or review the intended composition. Do not make a
  Field optional merely to bypass validation, and do not delete retained history.
  If the definition changed after preview, refresh before submitting again.

**Dropdown shows no choices**
: The field type is Single Select / Multi Select but no active Choice Set is
  assigned, or the assigned Choice Set has no active choices. A deleted or
  deprecated Choice Set is also not valid for new values. Assign an active
  Choice Set and add active choices through the Choice Set management surface.

**Custom field value is missing from exports**
: Ensure the custom field column is toggled on in the export column selector.
  Also verify the field is bound to the model type being exported.

**Fieldset not appearing in an Asset specification**
: Check the Asset's Type and its explicit Fieldset selection first, then the
  members' targets, activation and lifecycle. Target binding alone does not
  select a Fieldset. An explicitly empty composition remains empty.
