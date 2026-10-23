# Label Templates

A **Label Template** defines the printable layout and dimensions of labels for the object types it lists (assets, accessories, components, consumables, locations). See [Printing Labels](../../usage/labels.md).

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Barcode Format** | QR Code (every object type), Code 128 or Code 39 (assets only; encodes the asset tag). | String | Yes |
| **Description** | Optional notes on label format and printer compatibility. | Text | No |
| **Name** | The name of the label layout. | String | Yes |
| **Object Types** | The object types this template is offered for when printing. | Many-to-many | Yes |
| **Page Height** | Printable label height in inches. | Float | Yes |
| **Page Width** | Printable label width in inches. | Float | Yes |
| **Template Code** | Jinja2/HTML code specifying the graphical layout of the label. | Text | No |

## Features & Validation

* **Print Preview**: Generates print sheets scaled to label dimensions.
* **Dynamic Content**: Injects the object's label fields (for assets: tag, name, serial number, location, status) into the layout.

Custom label code is a Beta print-layout feature. Only superusers can create or
edit the global templates; rendering exposes a scalar field DTO rather than the
Django model, uses an immutable autoescaping Jinja sandbox, and sanitizes the
resulting HTML/CSS/resources before PDF generation.
