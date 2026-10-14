# Custody Templates

A **Custody Template** supplies the acceptance terms and signature settings used when an asset is checked out to an AssetHolder. A receipt snapshots the selected template's text and references the assignment for that custody period.

Templates may be scoped to a tenant, a tenant group, or globally. Checkout selects an active template for the AssetHolder's tenant and the asset's category. The category is required for this selection; a category's static EULA text is not a fallback.

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Category** | The asset category this template overrides for the specified scope. | Foreign Key | No |
| **Disclaimer** | Short disclaimer printed at the bottom of the signoff receipt. | Text | No |
| **Email Signature Request** | Sends an email containing a secure signature link to the holder. | Boolean | Yes |
| **EULA Text** | The legal terms of service shown to the end-user. | Text | Yes |
| **Is Active** | Deactivate to hide from choices | Boolean | Yes |
| **Logo** | Custom corporate logo printed on the receipt. | Image | No |
| **Name** | Descriptive name for this template (e.g. *Standard Laptop EULA*). | String | Yes |
| **QMS Reference** | Reference ID mapping to an internal Quality Management System document. | String | No |
| **Require Acceptance** | If checked, checkout creates a pending receipt; it does not delay checkout or assignment until signature. | Boolean | Yes |
| **Signature Provider** | The service handling the signature process (e.g., `local` for on-platform canvas, or plugin integrations like `docusign`). | String | Yes |
| **Tenant** | The specific Tenant scope that this template applies to. | Foreign Key | No |
| **Tenant Group** | The Tenant Group scope that this template applies to. | Foreign Key | No |

## EULA Overrides & Precedence

At checkout, ITAMbox looks for an active template matching the AssetHolder's tenant and the asset's category, then that tenant's group and the same category, then a global template for that category when `ALLOW_GLOBAL_CUSTODY_TEMPLATES` is enabled. There is no category-less tenant fallback and no fallback to EULA text stored on the Category. If no matching template requiring acceptance is found, this checkout does not create a custody receipt.

Checkout records the asset assignment before the holder accepts the receipt. A pending or declined receipt does not itself return the asset; operators must arrange and record check-in when the device is physically returned. See [Custody Receipts](custodyreceipt.md) for the signing and handoff lifecycle.
