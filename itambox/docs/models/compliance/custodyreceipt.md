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
| **IP Address** | The IP address of the device used to sign the receipt. | IP Address | No |
| **Qms Reference** | The qms reference of the custody receipt. | String | No |
| **Signature Canvas** | Base64 canvas stroke vector string representing the user's manual signature. | Text | No |
| **Signature Data** | The signature data of the custody receipt. | Text | No |
| **Signature Hash** | Cryptographic SHA-256 hash of the signature image data. | String | No |
| **Signature Provider** | Service handling the signature (e.g., `local` canvas or `docusign`). | String | Yes |
| **Signed At** | The timestamp when the digital signature occurred. | DateTime | Yes |
| **Token** | The token of the custody receipt. | String | Yes |
| **User Agent** | Browser header info logged at the time of signing. | Text | No |
| **Verification Hash** | SHA-256 digest of holder UPN, asset tag, signing timestamp, and submitted signature data. It is a consistency check, not a keyed integrity guarantee. | String | No |

## Verification & Audit Trails
The verification hash is `hashlib.sha256(f"{holder.upn}|{asset.asset_tag}|{signed_at.isoformat()}|{signature_data}".encode("utf-8")).hexdigest()`, so the timestamp enters the input in its full ISO-8601 form. IP address and user agent are stored separately and are not inputs to that digest. A plain SHA-256 digest can help detect an inconsistency with the recorded inputs, but does not prevent a privileged database editor from changing both the record and digest. Treat the receipt as electronic acceptance evidence and assess legal sufficiency with your organisation's legal advisers.

### Operator-prepared signing handoff

An operator with the custody-receipt preparation permission can prepare a signing session for a **pending** receipt and its intended holder. From the receipt detail view, the available handoff options include a link, QR code, and email. The recipient must authenticate as the intended holder before signing; the session is single-use and expires (30 minutes by default). A session is only valid while it has not been cancelled, not been consumed and not expired. Do not treat forwarding the link to another person as a transfer of signing authority. Exact button labels and navigation can vary with the deployed UI; use the actions shown on the receipt detail page.

Accepting or declining consumes the session. Declining changes the receipt state only; it does not check the asset in. After the device is returned, an operator must check it in through the normal assignment workflow. Accepted receipts cannot be signed again, and check-in or reassignment supersedes a still-pending receipt for that assignment. An operator holding the custody-receipt export permission can export an **accepted** receipt as JSON or PDF; other receipt states are not exportable through those routes.
