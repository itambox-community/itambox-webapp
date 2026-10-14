# Custom Fieldsets

A **Custom Fieldset** is an ordered group of custom field definitions. For Asset Types and Assets, a fieldset only contributes values when it is explicitly selected in the Asset Type's ordered specification composition. Field target bindings determine which object specification can use a member; they do not select the fieldset automatically. See [Custom Fields](../../usage/custom-fields.md#custom-fieldsets) for the authoring workflow.

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Namespace** | Part of the stable identity, paired with `slug`; defaults to `local`. | String | Yes |
| **Slug** | Stable machine identifier paired with `namespace`. Identity is immutable after creation. | String | Yes |
| **Label** | Section heading shown to users. This may change without changing the fieldset identity. | String | No |
| **Description** | Notes about the fieldset. | Text | No |
| **Fields** | Selected custom fields. Their membership and display position are stored separately. | Many-to-Many | No |
| **Field Positions** | Optional JSON map from stable Field keys to unique positive positions; omitted members follow selection order. | JSON | No |

## Features & Validation

* **Explicit Composition**: Asset Types select an ordered list of Fieldsets. Member target bindings determine applicability, not automatic selection.
* **History Preservation**: Removing a Fieldset from a Type does not erase values that become historical.
* **Managed definitions**: Core and library-managed fieldsets are read-only. Locally authored fieldset identity (`namespace` + `slug`) cannot be changed after creation.
* **Ordering**: Fieldset order on the Type and member position within each Fieldset determine presentation. A Field included more than once appears at its first placement.
