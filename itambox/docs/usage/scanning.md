# Scanning Assets

ITAMbox supports barcode and QR-code scanning in two modes: a **hardware scanner** connected to a desktop browser, and a **camera-based scanner** on mobile devices.

---

## Hardware / USB Barcode Scanners

Most USB / Bluetooth HID barcode scanners emulate a keyboard. To use one:

1. Open any asset audit session or the **Find by Scan** view (`/scan/resolve/`).
2. Click inside the scan input field so it has keyboard focus.
3. Configure your scanner to append **Enter** (newline) after each scan.
4. Scan the barcode — ITAMbox reads the input and resolves it automatically.

Supported code formats (handled by the resolve endpoint):

| Input format | Example | Notes |
|---|---|---|
| Bare asset tag | `IT-00042` | Case-insensitive |
| Serial number | `SN-ABC123` | Matched when no tag found |
| EAN / GTIN | `4012345678901` | EAN-8/13, GTIN-8/12/13/14, UPC — resolved through the AssetType catalogue (asset) or the inventory item's own EAN |
| Label reference | `itambox://assets.asset/42`, `itambox://inventory.accessory/7` | Printed on QR labels when `ITAMBOX_BASE_URL` is empty; identifies exactly one object |
| Detail URL | `https://itam.example.com/assets/assets/42/` | Printed on QR labels when `ITAMBOX_BASE_URL` is set |
| Legacy reference | `itambox://asset/42` | Pre-1.0 asset deep link |
| `itambox:` tag | `itambox:IT-00042` | Pre-1.0 QR labels; resolved like a bare asset tag |

!!! note "Tenant behavior"
    The **Find by Scan** flow searches **every tenant you can access**, no
    matter which tenant is currently selected. A user with no accessible
    tenant fails closed (404) — nothing ever leaks from tenants the user
    cannot access. Audit sessions and bulk-scan baskets remain bound to
    their session tenant: an EAN that matches an AssetType with **exactly
    one** asset in scope verifies that asset; if several assets share the
    type, the scan reports the ambiguity instead of picking one silently.

---

## Camera Scanning (Mobile)

ITAMbox can activate your device camera to scan barcodes and QR codes without a separate scanner app.

!!! warning "HTTPS required for camera access"
    Mobile browsers (iOS Safari, Android Chrome) only expose the camera API on **secure origins** (`https://`).  If ITAMbox is served over plain `http://`, the camera button will not appear and the browser will silently refuse the `getUserMedia()` call.

    **Production deployments must be served over HTTPS.** During local development you can use a self-signed certificate or a tool like [mkcert](https://github.com/FiloSottile/mkcert) to create a locally-trusted certificate.

To scan with the camera:

1. Open an audit session detail page or the **Find by Scan** view (`/scan/resolve/`).
2. Tap **Scan with Camera** (the camera icon in the action bar).
3. Grant camera permission when prompted.
4. Point the camera at a barcode or QR code — ITAMbox decodes it automatically and resolves the asset.

---

## Find by Scan & EAN Resolution (`/scan/`)

The **Find by Scan** view resolves any supported code or EAN to the matching target and redirects you to its detail page. The resolution follows a strict hierarchy (cross-tenant for every tenant you can access, and permission-gated):

1. **Label Match**: A label reference or detail URL of any labelled object (asset, accessory, component, consumable, location) redirects you to that object's detail page, provided you may view it. See [Printing Labels](labels.md).
2. **Asset Match**: If the code matches an `Asset` by asset tag or serial number, it redirects you directly to the asset's detail page. Asset tags are unique per tenant only: when the code matches assets in several of your tenants, you land on the asset list searched for that code instead of one of them.
3. **AssetType EAN Match**: If the code matches an `AssetType` EAN, it redirects you to the asset list view filtered to that EAN (`/assets/?ean=<ean>`).
4. **Inventory EAN Match**: If the code matches a `Component`, `Accessory`, or `Consumable` EAN, it redirects you directly to that inventory item's detail page.

If no object matches the scanned code, a 404 error is shown. Tenants you cannot access are never searched.

---

## Scanner-Driven Bulk Actions

ITAMbox supports scanner-driven bulk actions (check-out, check-in, and disposal) via a unified **Scan Basket** interface. This scales for high-volume transactions and survives request timeouts by running asynchronously.

### The Scan Basket Page
1. Navigate to the bulk scan basket page (e.g. `Bulk Check-in` at `/assets/bulk-checkin-scan/`, `Bulk Check-out` at `/assets/bulk-checkout-scan/`, or `Bulk Disposal` at `/assets/bulk-dispose-scan/`).
2. Add assets to the basket using the mobile camera scanner, keyboard-emulated USB barcode scanner, or manual entry.
3. The frontend (`scan-basket.ts` / `scanner.ts`) implements throttle limits to prevent duplicate scan bursts from registering.
4. Active warnings (e.g., "Already checked out", "Already disposed") are shown inline for ineligible scanned rows.

### Asynchronous Execution
Upon clicking submit, the web thread creates a background `Job` tracking record and enqueues the action into `django-q2` worker tasks:
*   **Bulk Check-in (`bulk_checkin_task`)**: Checks in the basket assets, allowing state overrides (deployable, pending, undeployable) and target location settings.
*   **Bulk Check-out (`bulk_checkout_task`)**: Checks out the assets to a single chosen target (holder, location, or parent asset) and sets expected return dates.
*   **Bulk Disposal (`bulk_dispose_task`)**: Disposes of assets, collecting WEEE compliance flags, data sanitization certificates, sanitized-by values, recipients, and asset-specific financial proceeds.

---

## Audit Session Scanning

During an audit campaign, the scan input is embedded in the audit session detail page. Each successful scan:

1. Resolves the code to an asset.
2. Records the asset as **physically verified** at the campaign location.
3. Updates the campaign's progress counters in real time (via HTMX).

Duplicate scans within the same session are deduplicated automatically.

---

## Action Bar Shortcuts

The scan action bar (visible on audit session pages) provides:

| Button | Action |
|---|---|
| **Scan Input** | Text field — type or paste a code, or let a HID scanner inject it |
| **Scan with Camera** | Activates the device camera (HTTPS required) |
| **Clear** | Resets the last scan result |

---

## Troubleshooting

**Camera button is missing or greyed out**
: The page is served over `http://`. Switch to `https://` or use mkcert for local dev.

**Scanner input is captured by the OS or another app**
: The scan input field must have keyboard focus. Click the field before scanning.

**Code resolves to the wrong asset**
: Check that the code format is supported (see table above). `itambox://` scheme codes are printed by the built-in label generator and are always unambiguous.

**"403 Forbidden" after a successful scan**
: The authenticated user lacks the `assets.view_asset` permission for the active tenant.
