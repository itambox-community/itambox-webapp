# Printing Labels

ITAMbox prints labels for **assets**, **accessories**, **components**, **consumables** and **locations**. Every one of them works the same way: the same print page, the same templates, the same code on the label, and the same scanners read it back.

---

## Printing

* **One object:** open its detail page and click the printer button next to **Edit**.
* **Several objects:** select rows in the list (or in a table on a detail tab) and click **Print Labels** in the selection toolbar.

Both open the print page, which lists what will be printed and asks for:

| Option | Meaning |
|---|---|
| **Label Template** | The saved templates for this object type, plus the **Built-in label** (2.25 x 1.25 in, QR code), which is always available. |
| **Layout** | **Continuous roll** (one label per page, sized to the template), **A4 sheet** or **US Letter sheet** (3 x 8 grid). |

Up to 25 labels are returned directly as a PDF. Larger selections (up to 1,000 per request) are printed in the background: you are taken to the job page, which links the finished PDF and notifies you when it is ready.

Printing needs the object type's **view** permission and only reaches objects in your current tenant scope. A background job re-checks access when it runs, so a permission revoked while the job waited is honoured.

---

## What the code on a label encodes

A label identifies **exactly one object**, everywhere:

| Setting | Encoded value | Read by |
|---|---|---|
| `ITAMBOX_BASE_URL` set (recommended) | The object's absolute detail URL, for example `https://itam.example.com/assets/assets/42/` | Any phone camera (opens the page) and every ITAMbox scanner |
| `ITAMBOX_BASE_URL` empty | `itambox://<app>.<model>/<id>`, for example `itambox://inventory.accessory/7` | Every ITAMbox scanner |

Asset tags are unique **per tenant only**; an MSP operator can reach several customers that use the same tag. A label therefore never relies on the tag to identify the asset. Labels printed before 1.0 carry `itambox:<tag>` and keep working: when a tag matches assets in several of your tenants, the scanner shows the matching assets instead of opening one of them.

**QR codes** carry the reference above and suit every object type. **Code 128** and **Code 39** cannot hold a URL; they encode the asset tag and are available for asset templates only.

---

## Label templates

Superusers manage templates under **Extras → Label Templates**. A template names the **object types** it prints, its size in inches, its barcode format and optional custom markup. Only templates listing an object type are offered when printing that type.

Custom markup is Jinja2/HTML rendered in a sandbox. It receives:

| Variable | Content |
|---|---|
| `obj` | The label's fields as text: `obj.title` plus the type's fields (see below) |
| `asset` | Same as `obj`, for asset templates written before 1.0 |
| `barcode_img` | A ready-to-use `<img>` tag with the code |
| `barcode_data_uri` | The code image as a `data:` URI |
| `reference` | The encoded reference (URL or `itambox://` form) |
| `model_name` | The object type, for example "accessory" |

Fields per object type:

| Object type | Fields |
|---|---|
| Asset | `asset_tag`, `name`, `serial_number`, `location`, `status` |
| Accessory, Component, Consumable | `part_number`, `manufacturer`, `category`, `ean` |
| Location | `site`, `status` |

Invalid custom markup falls back to the built-in card instead of failing the print run.

---

## Upgrading from a pre-1.0 release

* Existing templates are bound to **assets**, the only type the former engine printed.
* **Data Matrix** was never rendered (labels silently showed Code 128); such templates become **QR**.
* The asset-only routes (`/assets/<id>/print/`, `/assets/bulk-print-labels/`) and the generic `labels/select/…` page are replaced by the print page described above.
