# Bulk CSV & YAML Import Guide

## Introduction

ITAMbox provides a curated bulk importer for a declared allowlist of models;
it is not a general-purpose importer for every model. Supported forms accept
CSV or YAML input (CSV also supports comma, semicolon, or tab delimiters).
Updates are opt-in per import form and require an explicitly defined natural
key and change permission; this is not a general UPSERT guarantee. Confirmed
imports run as background jobs.

## Format, Delimiters & Text Pasting

### Supported Formats
- **CSV** (Comma-Separated Values)
- **YAML** (YAML Ain't Markup Language)

### Input Methods
- **File Upload**: Upload a `.csv` or `.yaml` file directly.
- **Text Pasting**: Paste CSV or YAML content into the import interface.

### Supported CSV Delimiters
| Delimiter | Symbol |
|-----------|--------|
| Comma     | `,`    |
| Semicolon | `;`    |
| Tab       | `\t`   |

### Encoding
- **UTF-8** (recommended)
- **UTF-8 with BOM** (Byte Order Mark)

> **Note**: Ensure your file is saved with the correct encoding to avoid character corruption, especially when using special characters or non-Latin scripts.

## Relational Field Resolution (Bilingual / Human-Friendly)

When importing data, relational fields (e.g., `site` on Location, `manufacturer` on AssetType) are resolved automatically. The system uses a three-tier lookup strategy to match values:

### Lookup Priority Order

1. **Primary Key (PK) Lookup** – Numeric ID
   - If the value is a numeric string (e.g., `"42"`), the system first attempts to find a record with that exact ID.
   - Example: `site: 42` → Looks for a Site with ID `42`.

2. **Exact Case-Sensitive Match**
   - If the value is not numeric, the system performs an exact case-sensitive match against the following fields (depending on the model):
     - `slug`
     - `name`
     - `model`
     - `username`
     - `upn`
   - Example: `manufacturer: "Dell Inc."` → Looks for a Manufacturer with `name` or `slug` exactly matching `"Dell Inc."`.

3. **Case-Insensitive Lookup**
   - If no exact match is found, the system performs a case-insensitive search across the same fields.
   - Example: `manufacturer: "dell inc."` → Matches `"Dell Inc."` if no exact match exists.

> **Important**: If no match is found after all three tiers, the import will fail for that row with a clear error message indicating the unresolved reference.

### Example: Resolving `site` on a Location

| Input Value | Lookup Result |
|-------------|---------------|
| `42`        | Site with ID `42` |
| `HQ-Office` | Site with `slug` = `"HQ-Office"` |
| `hq-office` | Site with `slug` = `"HQ-Office"` (case-insensitive fallback) |
| `Headquarters` | Site with `name` = `"Headquarters"` |

## In-place Updates

Import updates an existing record only when the form declares an **update key**, a natural key that identifies the record (never the internal `id`). A row whose key matches an existing record in your tenant scope updates it; every other row creates a new record. An `id` column, for example from an export, is ignored.

| Model | Update key |
|-------|-----------|
| **Asset** | `asset_tag` |
| **Manufacturer** | `name` |
| **Asset Holder** | `upn` |

All other importable models only create records.

### Permissions

Creating records requires the `add` permission. A row that would **update** an existing record additionally requires the `change` permission on that specific record. Without it the row fails with "You do not have permission to update existing objects" and nothing is written.

### Example: Updating an Asset

```csv
name,asset_tag,serial_number,notes
Server-01,SRV-001,SN123456,Updated notes
```

If an asset tagged `SRV-001` exists, it is updated with the new `name`, `serial_number`, and `notes`; otherwise a new asset is created.

## Asynchronous Background Processing

The import process follows a two-phase UI flow to ensure data integrity and provide real-time feedback.

### Phase 1: Preview

1. **Upload or Paste** your CSV/YAML data.
2. The system **validates** the format and parses the content.
3. A **preview table** displays the parsed rows with column headers.
4. Any **validation errors** (e.g., missing required fields, unresolved references) are highlighted.
5. You can review the data and make corrections before proceeding.

### Phase 2: Confirm & Process

1. Click **Confirm Import** to submit the data.
2. The system spawns an **asynchronous background task**.
3. A **Job** object is created to track the import progress.
4. You are automatically redirected to the **Job Detail view**, where you can monitor:
   - **Total rows** to process
   - **Rows processed** (completed)
   - **Rows succeeded** (successfully imported)
   - **Rows failed** (with error details)
   - **Current status** (Pending, Running, Completed, Failed)
5. The Job Detail view updates in **real-time** as the import progresses.

> **Tip**: You can navigate away from the Job Detail page and return later to check the status. The import continues in the background.

## Columns Supported per Model

### Asset

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | Asset name |
| `asset_tag` | No | String | Unique asset tag identifier (auto-generated from tag sequence if blank) |
| `serial_number` | No | String | Manufacturer serial number |
| `purchase_date` | No | Date | Date of purchase (YYYY-MM-DD) |
| `purchase_cost` | No | Decimal | Purchase cost/price |
| `order_number` | No | String | Purchase order number |
| `notes` | No | Text | Additional notes |

### Asset Type (specialized type importer)

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `manufacturer` | Yes | Relational | Manufacturer name, slug, or ID |
| `model` | Yes | String | Model name/number |
| `part_number` | No | String | Manufacturer part number |
| `category` | No | Relational | Asset type category |
| `asset_role` | No | Relational | Asset role |
| `description` | No | Text | Asset type description |
| `comments` | No | Text | Additional comments |
| `specs` | No | Structured mapping | Built-in specification keys and validated values; Asset Type importer only |

The Asset Type importer also accepts an optional structured `specs` mapping;
this is **not** a generic field of the bulk importer and is validated against
the built-in asset specification vocabulary. A minimal illustrative YAML
record is:

```yaml
manufacturer: Example Vendor
model: Example Laptop 14
category: laptops
specs:
  form_factor: notebook
  processor_model: Example CPU
  core_count: 8
  memory_capacity: "16 GiB"
  memory_type: ddr5
```

The Asset Type importer requires `manufacturer` and `model`. Existing types
with the same manufacturer and model are skipped, not updated. A missing
manufacturer is created only when the importer actor has
`assets.add_manufacturer`; an unknown category or asset role is an error and
the importer does not create either. Category should be an existing category
slug. With a category, its configured fieldset defaults are applied; without
one, the importer derives sections from supplied specification keys. The
preview validates the record before confirmation. `specs` keys, choices, and
canonical units are listed in the [Asset Type YAML authoring
guide](asset-type-catalogue.md); the upstream community contribution rules are
in the [type-library contributor
guide](https://github.com/itambox-community/type-library/blob/fd93a8be10a910f363f27ce6f327a32d790678ea/CONTRIBUTING.md).

This is the app's Asset Type import format, not the type-library catalogue
file contract (which additionally has catalogue/path conventions). It is also
separate from REST `specification_patch`/revision writes and from local custom
fields or fieldsets.

### Manufacturer

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | Manufacturer name |
| `slug` | No | String | URL-friendly identifier (auto-generated if empty) |
| `description` | No | Text | Manufacturer description |

### Location

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | Location name |
| `site` | Yes | Relational | Site name, slug, or ID |
| `slug` | No | String | URL-friendly identifier (auto-generated if empty) |
| `status` | No | String | Location status (e.g., Active, Inactive) |
| `parent` | No | Relational | Parent location name, slug, or ID |
| `facility` | No | String | Facility name |
| `description` | No | Text | Location description |

### Accessory

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | Accessory name |
| `manufacturer` | Yes | Relational | Manufacturer name, slug, or ID |
| `category` | No | String | Accessory category |
| `slug` | No | String | URL-friendly identifier (auto-generated if empty) |
| `part_number` | No | String | Manufacturer part number |
| `min_qty` | No | Integer | Minimum quantity threshold |
| `notes` | No | Text | Additional notes |

> **Note**: The `qty` (current quantity in stock) field is managed through the **AccessoryStock** model and is **not** directly importable on the catalog item. `AccessoryStock` is not supported by the generic bulk importer. Use the stock-management workflow or `POST/PATCH /api/inventory/accessory-stocks/`, with the required permissions.

### Consumable

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | Consumable name |
| `manufacturer` | Yes | Relational | Manufacturer name, slug, or ID |
| `category` | No | String | Consumable category |
| `slug` | No | String | URL-friendly identifier (auto-generated if empty) |
| `part_number` | No | String | Manufacturer part number |
| `min_qty` | No | Integer | Minimum quantity threshold |
| `notes` | No | Text | Additional notes |

> **Note**: The `qty` (current quantity in stock) field is managed through the **ConsumableStock** model and is **not** directly importable on the catalog item. `ConsumableStock` is not supported by the generic bulk importer. Use the stock-management workflow or `POST/PATCH /api/inventory/consumable-stocks/`, with the required permissions.

### License

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `name` | Yes | String | License name |
| `software` | Yes | String | Software name |
| `license_type` | No | String | Type of license (e.g., Perpetual, Subscription) |
| `product_key` | No | String | Product key or activation code |
| `seats` | No | Integer | Number of licensed seats/users |
| `purchase_date` | No | Date | Date of purchase (YYYY-MM-DD) |
| `purchase_cost` | No | Decimal | Purchase cost/price |
| `order_number` | No | String | Purchase order number |
| `expiration_date` | No | Date | License expiration date (YYYY-MM-DD) |
| `notes` | No | Text | Additional notes |

### Asset Holder

| Column | Required | Type | Description |
|--------|----------|------|-------------|
| `first_name` | Yes | String | First name |
| `last_name` | Yes | String | Last name |
| `upn` | Yes | String | User Principal Name (unique identifier) |
| `email` | No | String | Email address |
| `description` | No | Text | Asset holder description |
| `comments` | No | Text | Additional comments |

---

## Quick Reference: Required vs Optional Fields

| Model | Required Fields | Optional Fields |
|-------|----------------|-----------------|
| **Asset** | `name` | `asset_tag`, `serial_number`, `purchase_date`, `purchase_cost`, `order_number`, `notes` |
| **Asset Type** | `manufacturer`, `model` | `part_number`, `ean`, `category`, `asset_role`, `description`, `comments`, specialized `specs` mapping |
| **Manufacturer** | `name` | `slug`, `description` |
| **Location** | `name`, `site` | `slug`, `status`, `parent`, `facility`, `description` |
| **Accessory** | `name`, `manufacturer` | `category`, `slug`, `part_number`, `min_qty`, `notes` |
| **Consumable** | `name`, `manufacturer` | `category`, `slug`, `part_number`, `min_qty`, `notes` |
| **License** | `name`, `software` | `license_type`, `product_key`, `seats`, `purchase_date`, `purchase_cost`, `order_number`, `expiration_date`, `notes` |
| **Asset Holder** | `first_name`, `last_name`, `upn` | `email`, `description`, `comments` |

---

## Best Practices

1. **Use headers**: Include column headers in CSV. YAML must be one mapping or a list of mappings; it does not use a header row.
2. **Match column names exactly**: Column names are case-sensitive and must match the supported columns listed above.
3. **Clean your data**: Remove leading/trailing whitespace and ensure dates are in `YYYY-MM-DD` format.
4. **Use slugs for relational fields**: Slugs are URL-friendly and less prone to encoding issues than full names.
5. **Preview before confirming**: Always review the preview table to catch errors before starting the import.
6. **Monitor the Job**: After confirming, navigate to the Job Detail view to track progress and handle any failures.

---

## Import Path

Importable models that have both an explicit data-transfer declaration and a
curated import form are served by the generic route:

```
/import/<app_label>/<model_name>/
```

Examples of supported routes (the allowlist can differ by revision):
- `/import/assets/asset/` — Assets
- `/import/assets/assettype/` — Asset Types
- `/import/assets/manufacturer/` — Manufacturers
- `/import/inventory/accessory/` — Accessories
- `/import/inventory/consumable/` — Consumables
- `/import/organization/location/` — Locations
- `/import/organization/assetholder/` — Asset Holders
- `/import/licenses/license/` — Licenses
- `/import/subscriptions/subscription/` — Subscriptions

### Allowlist

Import is an **allowlist**. A model is importable only when it is declared importable in `core/data_transfer.py` and has a curated import form registered with `@register_import_form`; a contract test keeps both in lockstep and fails for any model without an explicit declaration. Every other model, including everything not listed above, answers **404** on the import route.

Records that a workflow owns (assignments, custody receipts, signing sessions, disposals, seat and stock assignments), authentication and access-control data, and system or generated records are never importable. Create them through their screens or the API so their lifecycle rules apply.
