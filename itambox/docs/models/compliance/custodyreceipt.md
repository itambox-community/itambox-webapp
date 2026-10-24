# Custody Receipts

A **Custody Receipt** records acceptance evidence for one asset assignment, including the holder, snapshotted terms and signature data. It is an application record; its presence does not establish legal validity in every jurisdiction or make the record tamper-proof.

---

## Acceptance States

A receipt is created only when checkout resolves a matching active template for the AssetHolder's tenant and the asset's category and that template has **Require Acceptance** set; the resolution order is documented on [Custody Templates](custodytemplate.md). The receipt is created in the **Pending** state and linked to that assignment. Checkout and assignment are already recorded; signature is a separate acceptance step.

- **Pending**: Awaiting acceptance from the intended holder. Assignment remains active.
- **Accepted**: The intended holder accepted the snapshotted terms for this assignment.
- **Declined**: The intended holder rejected the terms. The assignment remains active until an operator records a check-in or other appropriate action.
- **Superseded**: The custody period the receipt covers ended (check-in or reassignment) before the holder decided. The receipt can no longer be signed. Accepted and declined receipts are never changed by a check-in.

---

## Who Can Sign

Only the intended holder, signed in to ITAMbox. If the holder is linked to a login, only that login can sign. Otherwise ITAMbox applies the same exact-identity rule as SCIM provisioning: the login's username must equal the holder's UPN (case-insensitive), the e-mail address counts only when the holder has no UPN, and a login already linked to another holder of the same tenant is refused. A shared or recycled mailbox therefore never signs for a holder whose UPN differs.

---

## Attributes & Fields

| Field | Description | Type | Required |
| --- | --- | --- | --- |
| **Acceptance Method** | The acceptance method of the custody receipt. | String | Yes |
| **Acceptance Status** | The signature status (`pending`, `accepted`, `declined`, or `superseded`). | Selection | Yes |
| **Assignment** | The custody period (asset assignment) the receipt evidences. Set when the receipt is created. | Reference | No |
| **Accepted** | Read-only; true exactly when the Acceptance Status is `accepted`. Derived, not stored. | Boolean | No |
| **Accepted Date** | The accepted date of the custody receipt. | Date Time | No |
| **Asset** | The physical hardware checked out. | Foreign Key | Yes |
| **Created Date** | The created date of the custody receipt. | Date Time | No |
| **Custody Template** | The Custody Template rules used to generate this receipt. | Foreign Key | No |
| **Disclaimer** | The disclaimer of the custody receipt. | Text | No |
| **EULA Text** | The exact copy of the legal terms signed by the user. | Text | No |
| **EULA Version** | Version label stamped at signing. Constant (`1.0`) by design: the signed terms themselves are snapshotted in EULA Text, and custody templates carry no version. | String | Yes |
| **Holder** | The user or contractor taking custody. | Foreign Key | Yes |
| **Evidence Key ID** | Non-secret identifier of the key that sealed the receipt. | String | No |
| **Evidence Snapshot** | What the holder attested to, frozen at the decision: asset tag, serial number and holder UPN as they were at the time, hashes of the EULA, disclaimer and signature, client address and browser, and the decision time. Covered by the evidence seal. | JSON | No |
| **Evidence Version** | `1` for receipts sealed with a keyed HMAC; `0` for receipts decided before sealing existed (legacy). | Integer | Yes |
| **IP Address** | The client address of the device used to accept or decline, as resolved by the same proxy rules as rate limiting. | IP Address | No |
| **Qms Reference** | The qms reference of the custody receipt. | String | No |
| **Signature Canvas** | Base64 canvas stroke vector string representing the user's manual signature. | Text | No |
| **Signature Data** | The signature data of the custody receipt. | Text | No |
| **Signature Hash** | SHA-256 of the stored signature (the PNG data URI). | String | No |
| **Signature Provider** | Service handling the signature: `local` for the on-platform canvas, or a provider registered by a plugin. | String | Yes |
| **Signed At** | The timestamp when the digital signature occurred. | DateTime | Yes |
| **Token** | The token of the custody receipt. | String | Yes |
| **User Agent** | Browser user agent recorded when the holder accepts or declines (at most 512 characters). | Text | No |
| **Verification Hash** | The evidence seal: HMAC-SHA256 over the evidence snapshot, keyed by a key derived from the field-encryption keyring. On legacy receipts, an unkeyed SHA-256 digest. | String | No |

## Verification & Audit Trails

When the holder accepts or declines, ITAMbox freezes an **evidence snapshot** of what was attested and seals it. The seal (**Verification Hash**) is an HMAC-SHA256 over the snapshot. Its key is derived with HKDF from `ITAMBOX_FIELD_ENCRYPTION_KEYS`, separately from the encryption key itself; when the keyring is unset it is derived from `SECRET_KEY`, the same fallback field encryption uses. Someone who can edit the database but does not hold the key cannot rewrite a receipt and produce a matching seal.

A receipt verifies when the seal matches the snapshot under a configured key **and** the receipt row still agrees with the snapshot: holder, asset, assignment, template, provider, decision, signed-at time, terms, signature and client details. Later edits to the asset or holder, such as a renamed asset tag, do not affect verification, because the snapshot keeps the values as they were at the decision.

The receipt detail page, the JSON and PDF exports and the receipt API report one of these statuses:

- **Verified**: the seal and the row match.
- **Mismatch**: the receipt no longer matches its sealed evidence. The detail page names the parts that differ.
- **Unknown key**: the receipt was sealed with a key that is no longer in the keyring.
- **Legacy**: the receipt was decided before sealing existed. It carries an unkeyed digest that cannot be verified.
- **Not sealed**: the receipt is still pending or was superseded.

Run `manage.py verify_custody_receipts` to check every decided receipt; `--tenant <slug>` limits the check, and `--fail-on-mismatch` exits non-zero for monitoring. When you rotate `ITAMBOX_FIELD_ENCRYPTION_KEYS`, keep the old key in the keyring for as long as receipts sealed with it must verify; once it is removed, they report **Unknown key**. Treat the receipt as electronic acceptance evidence and assess legal sufficiency with your organisation's legal advisers.

### Operator-prepared signing handoff

An operator with the custody-receipt preparation permission can prepare a signing session for a **pending** receipt and its intended holder. From the receipt detail view, the available handoff options include a link, QR code, and email. The recipient must authenticate as the intended holder before signing; the session is single-use and expires (30 minutes by default). A session is only valid while it has not been cancelled, not been consumed and not expired. Do not treat forwarding the link to another person as a transfer of signing authority. Exact button labels and navigation can vary with the deployed UI; use the actions shown on the receipt detail page.

Accepting or declining consumes the session. Declining changes the receipt state only; it does not check the asset in. After the device is returned, an operator must check it in through the normal assignment workflow. Accepted receipts cannot be signed again, and check-in or reassignment supersedes a still-pending receipt for that assignment. An operator holding the custody-receipt export permission can export an **accepted** receipt as JSON or PDF; other receipt states are not exportable through those routes.
