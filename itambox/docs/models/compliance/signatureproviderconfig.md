# Signature Provider Configs

A **Signature Provider Config** holds the account details an external e-signature provider needs, such as an account ID and the secret it uses to authenticate its callbacks. External providers are installed by plugins; ITAMbox's own signature pad (`local`) needs no config.

A custody receipt that names an external provider uses the active config for that provider and the asset's tenant. When the tenant has none, it uses the install-wide config (one without a tenant). When neither exists, the signing page reports the provider as unavailable and nothing is sent to the provider.

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Tenant** | The tenant whose receipts use this config. Leave empty for the install-wide default; only platform administrators can create, change or delete an install-wide config. | Foreign Key | No |
| **Name** | Descriptive name. | String | Yes |
| **Signature Provider** | The provider this config is for. Only installed providers that need a config are offered. | String | Yes |
| **Is Active** | Inactive configs are ignored, and their callback URL stops accepting requests. | Boolean | Yes |
| **Options** | JSON object of non-secret settings the provider reads, such as an account or template ID. | JSON | No |
| **Secrets** | JSON object of credentials. Stored encrypted with `ITAMBOX_FIELD_ENCRYPTION_KEYS`, never shown again, and covered by `manage.py rotate_encryption_keys`. Leave the field blank when editing to keep the stored secrets; enter `{}` to remove them. Only the secret names are displayed. | JSON | No |

Each provider declares which options and secrets it needs; a config missing one is refused when saved. One active config per provider is allowed per tenant, plus one install-wide config per provider.

## How signing works with an external provider

1. Checkout e-mails the holder a link to ITAMbox's own signing page, never a provider link.
2. When the holder opens it, ITAMbox checks that they are the intended holder and that the custody period is still active. Only then does it ask the provider to start signing, and it redirects the holder there. The provider's request ID is stored on the receipt (**Provider Reference**), so opening the link again resumes the same request.
3. The provider reports the decision by calling the **Callback URL** shown on the config's detail page, or by sending the holder back to ITAMbox with a final result. The provider authenticates its callback itself, with a secret from this config. A callback can reach only receipts that this config started.
4. ITAMbox records the decision the same way as for the local pad: it re-checks that the receipt is pending and its period active, then seals the evidence, writes the change log and emits the receipt event. What the provider reports about the signer is kept as **Provider Evidence** and covered by the seal.

For plugin authors, the provider interface is described in the [plugin API reference](../../plugins/api_reference.md#custody-signature-providers-experimental).
