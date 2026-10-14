# Software Catalog

The **Software** model tracks software applications, operating systems, and developer tools approved for use inside your organisation (e.g. `Microsoft Office 365`, `Windows 11 Enterprise`, `Adobe Creative Cloud`). Each entry serves as a template under which multiple licenses and installations are tracked.

---

## Attributes

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Name** | Unique name of the software product (e.g. `Microsoft Visio Professional 2021`). Per-tenant uniqueness is enforced for active records. | String (255) | Yes |
| **Manufacturer** | The software developer or publisher (e.g. `Microsoft`, `Adobe`). | Foreign Key | Yes |
| **Version** | Current version string (e.g. `2021`, `16.0`). | String (50) | No |
| **Category** | Functional classification: `Operating System`, `Productivity`, `Development`, `Security`, `Design`, or `Other`. | Choice | No |
| **License Type** | Default license model: `Proprietary`, `Open Source`, `Freeware`, `Shareware`, or `Subscription`. | Choice | No |
| **Website** | Product homepage or vendor URL. | URL | No |
| **Description** | Optional free-text description of the software product. | Text | No |
| **Tenant** | Owning tenant. A null tenant denotes a shared/global catalogue entry visible to all tenants. | Foreign Key | No |
| **Tags** | Labels for categorisation and filtering. | M2M | No |

---

## Tenant Scoping

Software entries can be:
- **Tenant-scoped** (private to a single tenant): name uniqueness is enforced per tenant.
- **Global** (tenant is null): visible to all tenants, with global name uniqueness.

A tenant-owned software product can only be installed on assets of the same tenant; global software can be installed anywhere.

## Derived Counts

| Property | Description |
| --- | --- |
| `installed_count` | Number of `InstalledSoftware` instances referencing this catalog entry. |
| `license_count` | Number of active (non-deleted) `License` records linked to this software. |

## SAM Reconciliation

License reconciliation is provided by the module-level helper `licenses.reconciliation.reconcile_software(software)`; there is no `Software.reconcile()` model method. It compares installed `InstalledSoftware` counts with entitled license seats and returns a per-software `delta` (`entitled_seats - installed_count`) plus a status of `compliant`, `over_deployed`, or `unlicensed`. The helper does not evaluate version policy or whether an installation is authorised. See [Software Catalog & Maintenance](../../usage/software-catalog-and-maintenance.md).

## Relationship to Licenses

Each Software entry is a **catalog definition** — it is distinct from individual licenses and subscriptions. Multiple `License` records (each with seat counts, purchase details, and expiry dates) can reference a single Software entry, enabling SAM (Software Asset Management) reconciliation.
