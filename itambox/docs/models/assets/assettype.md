# Asset Types

An **Asset Type** represents a specific model of hardware manufactured by a vendor (e.g. `Dell Latitude 7440`, `Apple MacBook Pro 16-inch M3`, `Cisco Catalyst 9300`). It defines the model-level specification values, EOL limits, depreciation configurations, and custom metadata for its physical assets.

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Asset Role** | The functional role of the asset (e.g. `Developer Laptop`). | Foreign Key | No |
| **Category** | The asset category containing checkout rules. | Foreign Key | No |
| **Comments** | The comments of the asset type. | Text | No |
| **Custom Fieldsets** | Ordered specification sections composed for this model. | Many-to-Many | No |
| **Depreciation** | Straight-line depreciation rule template to apply. | Foreign Key | No |
| **Description** | The description of the asset type. | Text | No |
| **Ean** | Barcode (EAN / UPC / GTIN) — scanning shows assets of this type. | String | No |
| **EOL (Months)** | Useful lifespan in months before End-of-Life replacement is due. | Integer | No |
| **Model Image** | Product image for this asset type model. | Image | No |
| **Manufacturer** | The company that manufactured the hardware (e.g., `Apple`). | Foreign Key | Yes |
| **Model** | The specific model name or number (e.g., `MacBook Pro 16"`). | String | Yes |
| **Part Number** | Manufacturer part number or SKU | String | No |
| **Requestable** | Allows end-users to request assets of this type. | Boolean | Yes |
| **Slug** | Auto-slug source concatenating Manufacturer + Model. | Slug | Yes |

## Specifications

Asset Types compose reusable specification **fieldsets** into an explicit, ordered list of sections. A Type can start with its category's default sections, but the resulting composition can be edited. A fieldset is a group of field definitions; choosing a fieldset does not define the YAML keys or allowed values. See [Custom Fieldsets](../extras/customfieldset.md) for local field composition and [Custom Fields](../../usage/custom-fields.md) for field definitions and model/asset value behavior.

To add a model to the shared catalogue, follow the [Asset Type YAML authoring and import guide](../../integration/asset-type-catalogue.md). It lists the allowed specification keys, choice values, units, category slugs, preview requirements and skip behavior. This special catalogue importer is separate from local custom fields and REST specification writes.

The composed sections define the specification form of the model: model-level values are stored on the Asset Type and are not copied to its assets. Each asset records its own observed values per specification field, and values that become historical are retained.
