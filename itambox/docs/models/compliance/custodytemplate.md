# Custody Templates

A **Custody Template** supplies the acceptance terms and signature settings used when an asset is checked out to an AssetHolder. A receipt snapshots the selected template's text and references the assignment for that custody period.

Templates may be scoped to a tenant, a tenant group, or globally. Checkout selects an active template for the AssetHolder's tenant and the asset's category; a template without a category is the default for its scope. A category's static EULA text is not a fallback.

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Category** | The asset category this template applies to in its scope. Leave it empty to make the template the default for every category without a more specific template. | Foreign Key | No |
| **Disclaimer** | Short disclaimer printed at the bottom of the signoff receipt. | Text | No |
| **Email Signature Request** | Sends an email containing a secure signature link to the holder. | Boolean | Yes |
| **EULA Text** | The legal terms of service shown to the end-user. | Text | Yes |
| **Is Active** | Deactivate to hide from choices | Boolean | Yes |
| **Logo** | Custom corporate logo printed on the receipt. | Image | No |
| **Name** | Descriptive name for this template (e.g. *Standard Laptop EULA*). | String | Yes |
| **QMS Reference** | Reference ID mapping to an internal Quality Management System document. | String | No |
| **Require Acceptance** | If checked, checkout creates a pending receipt; it does not delay checkout or assignment until signature. | Boolean | Yes |
| **Signature Provider** | The service handling the signature process: `local` for the on-platform canvas, or a provider registered by a plugin. An external provider also needs an active [Signature Provider Config](signatureproviderconfig.md) for the asset's tenant or an install-wide one; without it the signing page reports the provider as unavailable. | String | Yes |
| **Tenant** | The specific Tenant scope that this template applies to. | Foreign Key | No |
| **Tenant Group** | The Tenant Group scope that this template applies to. | Foreign Key | No |

## EULA Overrides & Precedence

At checkout, ITAMbox looks for an active template matching the asset's category, first for the AssetHolder's tenant, then that tenant's group, then globally when `ALLOW_GLOBAL_CUSTODY_TEMPLATES` is enabled. Only when no scope has a template for that category does it look for a template without a category, in the same scope order. A category template in any scope therefore wins over a category-less one, and a category-less template also covers assets that have no category. Among several matching templates in one scope, the first by name wins. There is no fallback to EULA text stored on the Category. If no template resolves at all, or if the resolved template does not require acceptance, this checkout creates no custody receipt. Resolution selects the highest-priority matching template and then checks that one; it does not continue down the scope list looking for a template that requires acceptance.

Checkout records the asset assignment before the holder accepts the receipt. A pending or declined receipt does not itself return the asset; operators must arrange and record check-in when the device is physically returned. See [Custody Receipts](custodyreceipt.md) for the signing and handoff lifecycle.
