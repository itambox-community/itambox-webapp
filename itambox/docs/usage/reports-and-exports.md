# Reports & Exports

ITAMbox provides a flexible reporting and export system for getting data out
of the platform — whether that's a one-off CSV export of your asset inventory,
a scheduled PDF report delivered by email every Monday morning, or printed
QR-code labels for your server rack.

---

## Export Templates

**Export Templates** define custom downloadable formats for any model's list
data. Instead of being limited to the default CSV export, you can create
templates that produce JSON, XML, custom CSV layouts, or any text-based format.

### Creating an Export Template

Navigate to **Extras → Export Templates** and click **Add**:

| Attribute | Description |
|-----------|-------------|
| **Name** | Unique identifier (e.g. "Asset CSV for Finance") |
| **Content Type** | The model this template exports (e.g. `assets \| asset`) |
| **Template Code** | Jinja2 template rendered over the full query result set |
| **MIME Type** | HTTP Content-Type header (e.g. `text/csv`, `application/json`) |
| **File Extension** | Download filename extension (e.g. `csv`, `json`, `xml`) |
| **Description** | Optional notes on what the template produces |
| **Download as attachment** | Serve as file download (default) or display inline in browser |

### Template Code

The template receives a single variable — `queryset` — containing the full,
filtered result set. The template author is responsible for iterating rows and
emitting any header:

```jinja2
{# CSV Export Template #}
Asset Tag,Name,Status,Purchase Date
{% for asset in queryset %}
"{{ asset.asset_tag }}","{{ asset.name }}","{{ asset.status }}","{{ asset.purchase_date }}"
{% endfor %}
```

For JSON exports:

```jinja2
[
{% for obj in queryset %}
  {
    "id": {{ obj.id }},
    "tag": "{{ obj.asset_tag }}",
    "name": "{{ obj.name }}"
  }{% if not loop.last %},{% endif %}
{% endfor %}
]
```

> [!IMPORTANT]
> Export templates are rendered in a **sandboxed Jinja2 environment**.
> Dangerous filters (`|attr`, `|format`, `|map`, `|pprint`) and globals
> (`cycler`, `joiner`, `namespace`, `lipsum`) are disabled. Sensitive Python
> dunder attributes are blocked. This is defence-in-depth — only superusers
> can author templates, but the sandbox prevents accidental or malicious
> server-side code execution.

### Security: CSV Formula Injection

Use the built-in `|csv_safe` filter to neutralise spreadsheet formula injection
when exporting to CSV/Excel formats. Wrap any field that might start with `=`,
`+`, `-`, or `@`:

```jinja2
"{{ asset.name|csv_safe }}","{{ asset.notes|csv_safe }}"
```

---

## Label Templates

**Label Templates** define the printable layout for physical asset tags,
barcodes, and QR code labels. They control both the label dimensions and the
content printed on each label.

### Creating a Label Template

Navigate to **Extras → Label Templates** and click **Add**:

| Attribute | Description |
|-----------|-------------|
| **Name** | Label layout name (e.g. "Avery 5160 — Asset Tags") |
| **Page Width** | Label width in inches (e.g. `2.25`) |
| **Page Height** | Label height in inches (e.g. `1.25`) |
| **Barcode Format** | Symbology for the printed code |
| **Template Code** | Jinja2/HTML template for label content and layout |
| **Description** | Optional printer/stock compatibility notes |

### Supported Barcode Formats

| Format | Slug | Best For |
|--------|------|----------|
| **Code 128** | `code128` | General-purpose asset tags — dense, supports alphanumeric, widely supported by scanners |
| **Code 39** | `code39` | Legacy systems, simple alphanumeric, lower density |
| **QR Code** | `qr` | Mobile scanning, URLs, large data payloads (up to ~4K chars) |
| **Data Matrix** | `datamatrix` | Small labels, industrial/PCB marking, high data density in small footprint |

### Printing Labels

1. Navigate to the asset list view.
2. Select the assets you want to label (checkbox selection).
3. Click the **Labels** action button in the list toolbar.
4. Choose a **Label Template** from the dropdown.
5. ITAMbox generates a print sheet with one label per selected asset.
6. Use your browser's print dialog (Ctrl+P / Cmd+P) to print at 100% scale.

> [!WARNING]
> Set your browser print settings to **no margins** and **100% scale**.
> Page scaling, "fit to page", or default browser margins will misalign
> labels on the physical sticker sheet. Always test-print on plain paper
> first.

### Template Code Variables

Label templates receive a deliberately small scalar DTO — not the Django `Asset`
model. Supported values are:

- `{{ asset.asset_tag }}` — the unique asset tag (encoded in the barcode)
- `{{ asset.name }}` — asset display name
- `{{ asset.serial_number }}` — manufacturer serial number
- `{{ asset.location }}` — recorded base/storage location name, not a live position
- `{{ asset.status }}` — current status name
- `{{ barcode_data_uri }}` — the internally generated barcode image URI
- `{{ barcode_img }}` — the internally generated barcode image element
- `{{ barcode_format }}` — the selected symbology

Rendered output is sanitized before PDF generation. Scripts, event handlers,
external resources, unsafe CSS, and model/dunder access are not supported.

---

## Report Templates

**Report Templates** define the content, layout, and styling of compiled
system reports. They are used both for on-demand report generation and as
the basis for scheduled reports.

### Stable availability

The **Report Designer** is **Stable** and always available. Users with the
applicable permissions can use the template list, detail, edit, preview, and
download surfaces; no activation setting is needed. See
[Capability Maturity](../operations/capability-maturity.md) for the declared
grade and activation model. The curated report catalogue is a separate Stable
capability.

### Creating a Report Template

Navigate to **Extras → Report Templates** and click **Add**:

| Attribute | Description |
|-----------|-------------|
| **Name** | Unique template name |
| **Report Type** | The data set to compile (see below) |
| **Included Columns** | Checked columns rendered in the report data grid |
| **Include Summary Cards** | Show/hide top-level KPI cards (totals, counts, sums) |
| **Include Distribution Chart** | Embed a distribution chart in the HTML report |
| **Group By Field** | Optional column to group grid rows under (e.g. `location`, `status`) |
| **Style Preset** | Visual layout for HTML/PDF renders |
| **Filter Tenants** | Limit data to selected tenants (blank = aggregate within the effective scope) |
| **Description** | Optional notes |

Each report provider declares the domain permissions for its data, and the
compiler enforces all declared permissions for every tenant in the effective
compile scope. For example, Hardware Inventory requires access to accessories,
consumables, and components. Missing or unresolved authorization fails closed.
This applies centrally to preview, download, and scheduled compilation. A
denied preview or download returns HTTP 403. A denied scheduled run records
`terminal: report.permission_denied`, creates no archive, and sends no report.
The Stable grade does not change these checks.

### V1 template contract

The Stable V1 contract freezes each template's report type, included columns,
filters, and grouping. The column picker uses the selected provider's declared
catalogue. The Asset Disposal report also offers `disposal_status`,
`disposal_cancelled_at`, `disposal_cancelled_by`, and
`disposal_cancellation_reason`. Unknown or non-canonical column keys are
rejected when a template is saved. A canonical key that the selected provider
does not support leaves that cell blank; an unsupported grouping falls back to
the provider's default grouping.

CSV exports always contain the selected columns and compiled rows, followed
by a disclosure row when the output is sample-only or truncated. Optional
custom HTML/Jinja templates remain supported through the restricted rendering
sandbox. The sandbox is immutable and removes the `attr`, `format`, `format_map`,
`map`, `pprint` and `xmlattr` filters and the `cycler`, `joiner`, `namespace` and
`lipsum` globals, matching export templates. The rendered HTML is then reduced to
an allowlist of presentational markup before it is shown, downloaded or emailed:
`script`, `style`, frame, form and SVG elements, `on*` handlers, inline styles and
`javascript:` URLs are removed whatever the Content-Security-Policy says.
Images must not load from other hosts: an `<img src>` is kept only for an embedded
`data:image/png|jpeg|gif;base64,...` URI, a relative path such as `/static/logo.png`
or `/media/logo.png`, or (when `ITAMBOX_BASE_URL` is set) an absolute URL on that
origin under the static or media path. `http(s)` images on other hosts, `//host`
and UNC paths are removed, because mail clients and downloaded HTML copies would
otherwise send the rendered report data and the reader's IP address to a third
party. Use a data URI or a tenant-hosted static/media asset for logos. Links
(`<a href>`) are unaffected.

### Tenant scope authorization

The pinned-scope reach check and the report's domain permissions are separate
checks. A single pinned tenant must be within the actor's reach. A scope pinned
to multiple tenants requires `reports.view_cross_tenant_reports` for every
pinned tenant, then every declared report permission for every pinned tenant.
An aggregate compile without a pinned scope is evaluated against the same
effective tenant scope the scoped querysets read: the canonical accessible set
under **All accessible tenants**, or the accessible tenants of the active
tenant group. Every declared report permission is required for every tenant in
that effective scope; a member context that resolves no scope reads no rows
and fails closed. Only a truly global (unscoped) context is evaluated against
every live tenant. Tenant reach, `reports.view_cross_tenant_reports`, and
Report Template permissions do not grant access to report domain data by
themselves.

An active superuser passes the domain permission check. `Run now` always
evaluates the user who triggered it: their tenant reach for the persisted
scope and the provider's declared domain permissions both decide, for
single-tenant and broad schedules alike, and a stored scope approver never
widens what an interactive run may compile. An unattended broad run evaluates
its recorded scope approver. An unattended single-tenant scheduled run has no
acting user and compiles only under explicit system authorizations for the
provider's declared permissions. Missing or unresolved authorization fails
closed.
Preview and download denials return HTTP 403. A denied scheduled run records
`terminal: report.permission_denied`, creates no archive, and is not delivered.
This is an intentional access change for principals who previously had report
template or schedule access without the required domain permissions.

### Available Report Types

| Report Type | Slug | What It Contains |
|-------------|------|-----------------|
| Asset Inventory Summary | `asset_summary` | Full asset inventory with status, location, financials |
| License Utilization | `license_utilization` | License seats purchased vs assigned, compliance gaps |
| Subscription Renewals | `subscription_renewals` | Upcoming subscription expirations, costs, renewal contacts, agreement entitled quantity |
| Asset Maintenance & Repairs | `asset_maintenance` | Maintenance history, open repair tickets, costs |
| Asset Depreciation Summary | `asset_depreciation` | Book values, depreciation schedules, GWG write-offs |
| Software Catalog & Installations | `software_inventory` | Installed software, versions, licensing status |
| Contract Renewals & Expirations | `contract_renewals` | Vendor contracts nearing expiry, value, auto-renewal flags |
| Warranty Expiration | `warranty_expiration` | Assets with warranties expiring in configurable windows, linked warranty Supplier |
| Asset Disposal & End-of-Life | `asset_disposal_eol` | Disposed assets, WEEE compliance, data sanitization records |
| Hardware Inventory | `hardware_inventory` | Accessories, consumables, components, stock levels |
| Custody & EULA Compliance | `custody_compliance` | Asset custody sign-offs, EULA acceptance tracking |

Warranty Expiration surfaces the linked warranty Supplier. Subscription Renewals surfaces the agreement entitled quantity; license seat totals remain exclusive to License Utilization.

### Style Presets

| Preset | Slug | Appearance |
|--------|------|-----------|
| **Executive (Branded)** | `default` | Indigo brand band, accented summary cards — for leadership |
| **Compact (Dense)** | `compact` | Dense rows with zebra striping — for audit and operations lists |
| **Financial (Ledger)** | `financial` | Stone ledger with emphasised monetary totals and tabular figures |
| **Minimal (Clean)** | `minimal` | Clean black-on-white, single indigo hairline — for forwarding, embedding, or printing |

### Output window and disclosure

Each report provider compiles at most 500 rows. Hardware Inventory applies
that limit separately to accessories, consumables, and components. When a
provider's window is capped, or an empty scope is rendered with sample data,
the output identifies that it is truncated or a sample: HTML/PDF includes a
notice banner, ordinary CSV appends a trailer row, XLSX appends a note row, and
scheduled mail includes the notice. Download responses include the applicable
`X-Report-Truncated`, `X-Report-Row-Window`, `X-Report-Total-Rows`, and
`X-Report-Sample` headers.

Machine-format exports (`machine_csv` and specification exports) contain no
in-file disclosure rows and cover the compiled window. Their download
responses still carry the applicable disclosure headers. Requesting
`machine_csv` for a report without a machine-format export returns HTTP 400.

---

## Scheduled Reports

**Scheduled Reports** automatically compile a Report Template on a recurring
schedule and deliver the result via email or notification channels. The
capability is **Stable** and always available; the only setup it needs is a
schedule. Deactivating a schedule pauses its delivery without deleting the
row. Delivery depends on a running `qcluster` worker. Upgrading a deployment
that ran with the designer flag disabled pauses schedules that were being
skipped instead of resuming them silently; see
[Updating a deployment](../operations/upgrades.md) for the transition and how
to resume a schedule. See
[Capability Maturity](../operations/capability-maturity.md) for the declared
grade and activation model.

### Creating a Scheduled Report

Navigate to **Extras → Scheduled Reports** and click **Add**:

| Attribute | Description |
|-----------|-------------|
| **Name** | Display name for this schedule |
| **Report** | The Report Template to compile |
| **Frequency** | How often to run (see below) |
| **Cron Expression** | Custom cron string (only when Frequency = Custom Cron) |
| **Start Time** | Time of day to execute (e.g. `08:00:00`) |
| **Format** | Delivery format |
| **Recipients** | Comma-separated email addresses |
| **Channels** | Notification channels to deliver through (optional) |
| **Save To Archive** | Keep a copy of each generated report |
| **Filter Tenants** | Scope report data to specific tenants |
| **Is Active** | Enable/disable this schedule |

### Frequencies

| Frequency | When It Runs |
|-----------|-------------|
| **Once** | Single execution at the next Start Time |
| **Hourly** | Every hour |
| **Daily** | Every day at Start Time |
| **Weekly** | Every week on the same day |
| **Biweekly** | Every two weeks |
| **Monthly** | Once per month |
| **Quarterly** | Every three months |
| **Yearly** | Once per year |
| **Custom Cron** | Arbitrary cron expression (e.g. `0 8 * * 1-5` for weekdays at 8 AM) |

### Delivery Formats

| Format | Slug | Delivery Method |
|--------|------|----------------|
| **HTML Email** | `html` | Rendered report inline in the email body |
| **CSV Attachment** | `csv` | CSV file attached to the email |
| **PDF Attachment** | `pdf` | PDF rendered via xhtml2pdf, attached to the email |
| **Excel (XLSX) Attachment** | `xlsx` | Excel workbook via openpyxl, attached to the email |

### Notification Channels

In addition to email delivery via the **Recipients** field, scheduled reports
can be attached to one or more **Notification Channels** (configured under
Extras → Notification Channels). This enables delivery to webhooks, Slack,
Microsoft Teams, or other integrated platforms.

### Scheduling Contract

The Stable scheduling contract is frozen for V1:

| Aspect | Behavior |
|--------|----------|
| Frequencies | Once, hourly, daily, weekly, biweekly, monthly, quarterly, yearly, and custom cron. |
| Cron validation | A custom cron expression is validated when the schedule is saved; an invalid expression is rejected. |
| Time zone | All cadence math runs in the deployment's cluster time zone (the Django `TIME_ZONE` setting). Daily, weekly, monthly, and quarterly cadences keep their local wall-clock time across daylight-saving changes; monthly, quarterly, and yearly cadences clamp to month end (the 31st becomes the 28th in February). |
| Missed runs | If the worker was down while occurrences came due, each scheduling pass replays the oldest missed occurrence once before advancing the cadence, so a backlog is worked off one occurrence per pass and never floods the queue. |
| Next run | The list view shows the next scheduled execution per active schedule, calculated from the stored start time and cadence. |
| Schedule changes | Editing a schedule's cadence re-anchors the next run from the start time; editing only metadata (for example recipients) keeps the live next run. Deactivating removes the background row; reactivating re-registers it with a fresh anchor. |
| Concurrent runs | Occurrences are independent: a run that is still executing when the next occurrence comes due does not block it, and each run is identified by its intended occurrence time, which is what deduplicates redelivery. |
| Redelivery and limits | A redelivery of the same occurrence is a recorded no-op and idempotency is tracked per occurrence, so an out-of-order replay (a newer occurrence accepted first) never discards an older one, and a stuck or crashed run is never dispatched twice. If a run stops after claiming its occurrence, that occurrence is not re-run automatically: the archive and the run status show how far it got, **Retry delivery** recovers failed deliveries, and the next occurrence runs normally. The contract prioritizes never sending a duplicate over guaranteed completion. |

### Delivery Outcomes and Retry

Each run is observable per stage:

- **Generation and archive**: the run either compiles and archives (when
  **Save To Archive** is on) or records a generation failure. A failed
  generation attempts no delivery at all.
- **Delivery ledger**: the archive row of a run carries a per-target ledger:
  one aggregate entry for the email recipients and one entry per attached
  notification channel, each marked delivered or failed. The schedule list
  shows the run outcome (`success`, `partial`, or `failed`).
- **Retry delivery**: the action appears for schedules whose latest run ended
  `partial` or `failed` (a generation failure is recovered with **Run now**).
  It re-attempts exactly the targets recorded as failed, never
  re-sends targets that already succeeded, and contacts the recorded original
  recipients, email subject/body, and notification payloads even if the
  schedule was edited since. It is refused while the schedule is inactive, and
  it re-checks the archived run's generation scope against the standing
  approval before contacting anything, so a later scope change cannot
  legitimize an older export. The retry replays the newest run's own archive
  only: a run that retained no archived output is refused instead of
  redelivering an older report — use **Run now** to generate a fresh run.
  Parallel retry requests are serialized by an exclusive, self-expiring claim;
  the fan-out renews it before every target, aborts once it was lost, and
  outbound attempts are bounded, so overlap is limited to a single in-flight
  send (best-effort duplicate suppression).

### Cross-Tenant Scope Approvals

A schedule whose **Filter Tenants** scope spans more than one tenant compiles
cross-tenant data and therefore requires a durable scope approval. The schedule
list shows the scope state per schedule; the **Scope Approval** page
(**Extras → Scheduled Reports → Scope**) names every tenant in scope and the
current approval.

- Approving or revoking requires the `reports.view_cross_tenant_reports`
  permission on every tenant in scope; an approval by a principal whose reach
  does not cover the full scope is refused.
- A current approval is required for a cross-tenant schedule. Generation also
  checks authorization at compile time; an unauthorized generation records
  `terminal: report.permission_denied` and is not delivered. Changing the
  scope after an approval invalidates it, as does revoking it; **Retry
  delivery** re-checks the archived run's generation scope against the
  standing approval before it re-contacts any failed target, so a narrowed
  re-approval never authorizes an older, broader export.
- Revocation keeps the approval history visible and marks it void; approving
  again records a fresh approval.

### Monitoring

Each scheduled report tracks execution state:

| Attribute | Description |
|-----------|-------------|
| **Last Run** | Timestamp of the most recent execution |
| **Last Status** | Run outcome token: `success`, `partial`, or `failed`; a failed compilation appends the `report.generation_failed` detail |
| **Next Run** | Next scheduled execution for active schedules |

When a Scheduled Report is deleted, its linked background task schedule is
automatically cleaned up to prevent orphaned cron jobs.

---

## Saved Filters

**Saved Filters** let you capture and reuse list view filter configurations.
Instead of re-entering the same filters every time you view the asset list,
save them once and apply them with a single click.

### Saving a Filter

1. Navigate to any list view (e.g. Assets, Licenses, Subscriptions).
2. Apply your desired filters using the filter panel.
3. Click the **Save Filter** button (bookmark icon) in the filter bar.
4. Give the filter a **Name** and optional **Description**.
5. Choose whether to **Share** it (visible to all tenant members) or keep it
   private.

> [!TIP]
> Saved Filters store the query parameters, not the result set. Applying a
> saved filter re-runs the query against the current database state, so
> results are always up-to-date.

### Applying a Saved Filter

From any list view:

1. Click the **Saved Filters** dropdown in the filter bar.
2. Select a filter from the list.
3. The list view reloads with the saved filter parameters applied.

The active filter name is displayed in the filter bar, and you can clear it
by clicking the **Reset** button.

### Managing Saved Filters

Navigate to **Extras → Saved Filters** to:

- **Edit** a filter's name, description, or sharing setting
- **Disable** a filter without deleting it — it disappears from the dropdown
- **Delete** filters that are no longer needed

### Filter Scope

| Scope | Visibility |
|-------|-----------|
| **Shared** (default) | Visible to all members of the owning tenant |
| **Private** (`shared` unchecked) | Visible only to the creator |
| **System-wide** (`tenant` is null) | Visible across all tenants — superusers only |

---

## The Export Workflow

The standard export workflow — from list view to downloaded file — works as
follows:

1. **Navigate** to a list view (e.g. **Assets**, **Licenses**, **Subscriptions**).
2. **Filter** the list to the subset of records you want to export. Use the
   filter panel, saved filters, or search to narrow the result set.
3. **Click Export** — the export button (download icon) is in the list view
   toolbar.
4. **Select an Export Template** from the dropdown. The default "CSV Export"
   template is always available; custom templates you've created appear below.
5. **Choose a file format** if prompted — some templates offer format options
   (CSV, JSON).
6. **Download** — the file is rendered server-side and served as a browser
   download.

> [!IMPORTANT]
> Exports respect the **current filter state** of the list view. Only rows
> that match the active filters are included in the export. To export the
> full dataset, clear all filters before clicking Export.

### Which Models Can Be Exported

Generic CSV/YAML/template export is **explicit opt-in**. A model is exportable
only when it carries a reviewed data-transfer declaration (`core.data_transfer`);
every other model — including framework tables, authorization metadata, personal
records, generated logs and models whose export is owned by a dedicated surface
— answers **404** on every generic path (CSV, YAML, template render, and the
`all`, `filtered` and `pk=` scopes alike). There is no default-allow fallback:
adding a model without a declaration makes it non-exportable, and the contract
test fails the build until a decision is recorded.

Each exportable model declares how its rows are restricted, and the export
always applies that restriction:

| Declared scope | Rows you receive |
|----------------|------------------|
| Tenant-scoped manager | The rows of your active tenant scope (single tenant, tenant group, or all accessible tenants) |
| Shared reference data | The tenantless reference rows every tenant sees (for example manufacturers or asset types) |
| Container-scoped | The rows of the tenants and providers you hold the view permission for |
| Personal | Only your own rows — never another user's |

`export_scope=all` means **every row you may see under your current scope**, not
every row in the database; `export_scope=filtered` additionally applies the
list filters, and a `pk=` list is intersected with the same scope. Superusers
pass these gates by platform convention and see every tenant's rows.

Dedicated export surfaces stay authoritative: custody receipts, for example, are
exported only through their own JSON/PDF export, which requires the dedicated
`export_custodyreceipt` permission and applies asset-tenant scoping. Where a
model keeps a generic export *and* declares a dedicated export permission, the
generic route requires both permissions, so it is never the weaker door.

> [!NOTE]
> If an export link you used before now returns 404, the model's export is no
> longer part of the generic surface — the reviewed inventory removed it (for
> example because it holds signature or credential material, is personal data,
> or is a system log). Export templates that were authored for such a model are
> hidden from the content-type picker and fail closed instead of downloading an
> empty or over-broad file.

### Export File Naming

Downloaded files are automatically named using the pattern
`{model}_export.{extension}` — for example, `asset_export.csv` or
`license_export.json`. The filename is ASCII-safe to work across all
operating systems.

---

## Troubleshooting

### Export / Report Issues

**Export is empty or missing columns**
: Verify your active list filters are not too restrictive. Check that
  the export template's **Content Type** matches the model you are exporting.
  For report templates, ensure the **Included Columns** list has the columns
  you expect checked.

**Label print is misaligned or scaled wrong**
: Check browser print settings — margins must be **None**, scale must be
  **100%**. Also verify the label template's **Page Width** and **Page Height**
  match your physical label sheet dimensions exactly.

### Scheduled Report Issues

**Report never runs**
: Check that **Is Active** is toggled on. Verify the `django-q2` cluster is
  running (check the Django admin Q cluster page). Ensure the **Start Time**
  is in the future and the worker is not backlogged.

**"Cron expression is required" validation error**
: When **Frequency** is set to **Custom Cron**, the **Cron Expression** field
  cannot be blank. Provide a valid 5-field cron expression (e.g. `0 9 * * 1`
  for every Monday at 9 AM).

**"Invalid Cron expression" validation error**
: The cron string does not parse. Verify format: `minute hour day month weekday`
  (5 fields, space-separated). Use a tool like [crontab.guru](https://crontab.guru)
  to validate your expression.

**"Not a valid email address" validation error**
: One or more addresses in the **Recipients** field fail email format
  validation. Check for typos, missing `@` signs, or trailing commas.
  Addresses must be comma-separated: `alice@example.com, bob@example.com`.

**Report delivered but empty or wrong data**
: Check the **Filter Tenants** setting — if specific tenants are selected,
  only data from those tenants is included. Verify the underlying Report
  Template has the correct **Report Type** and **Included Columns**.

### Saved Filter Issues

**Saved filter dropdown is empty**
: All saved filters are either disabled or scoped to another tenant. Check
  Extras → Saved Filters — ensure at least one filter is **Enabled** and
  matches the current tenant context.

**"Filter already exists" when saving**
: A saved filter with the same name already exists for this model type and
  tenant. Choose a different name or delete/rename the existing filter.
