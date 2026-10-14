# Discovery sync

ITAMbox can pull device inventory from Microsoft Intune. Other discovery systems
can integrate through the REST resources documented below; the example is a
lookup-then-create workflow, not a server-side upsert.

---

## 1  Microsoft Intune connector

### 1.1  Azure app registration

1. In the Azure portal, go to **Entra ID → App registrations → New registration**.
2. Give it a name (e.g. *ITAMbox Intune Sync*), choose **Accounts in this
   organizational directory only**, and click **Register**.
3. In the app's **Certificates & secrets** blade, create a **Client secret**.
   Copy the value now — it is shown only once.
4. In **API permissions → Add a permission → Microsoft Graph → Application
   permissions**, add:
   - `DeviceManagementManagedDevices.Read.All`
5. Click **Grant admin consent** for your organisation.
6. Note the **Application (client) ID** and **Directory (tenant) ID** from the
   app's Overview blade.

### 1.2  Environment variable

Set `ITAMBOX_TENANT_INTUNE_CONFIGS` to a JSON object keyed by ITAMbox **tenant
slug**:

```json
{
  "acme": {
    "azure_tenant_id": "<Directory tenant ID from Azure>",
    "client_id": "<Application client ID from Azure>",
    "client_secret": "<Client secret value>",
    "create_missing": true,
    "default_status": "deployable",
    "sync_software": true
  }
}
```

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `azure_tenant_id` | string | required | Azure Directory (tenant) ID |
| `client_id` | string | required | Azure Application (client) ID |
| `client_secret` | string | required | Azure client secret value |
| `create_missing` | bool | `false` | Create an Asset when no tenant-local serial-number match exists |
| `default_status` | string | `"deployable"` | StatusLabel slug assigned to newly created assets |
| `sync_software` | bool | `true` | Process detected apps into InstalledSoftware records |

Secrets stay in the environment — never store them in the database. The keys
are lowercase and case-sensitive; uppercase variants are not aliases. The
`globex` example below is a separate tenant configuration, not a provider token
that can cross tenant boundaries.

Multiple tenants are supported by adding additional top-level keys:

```json
{
  "acme":   { "azure_tenant_id": "...", "client_id": "...", "client_secret": "..." },
  "globex": { "azure_tenant_id": "...", "client_id": "...", "client_secret": "..." }
}
```

### 1.3  Running a sync

**Run synchronously (for example from an OS scheduler):**

```bash
python manage.py sync_intune --tenant acme --now
```

`--dry-run` simulates the sync without writing. The command requires a configured
tenant and a superuser for change attribution. Without `--now`, it creates a Job
and enqueues the sync with django-q2; `--now` runs it inline and blocks until
finished.

**Schedule nightly:**

Use your host/container scheduler to run the synchronous command once per
night, for example at 03:00:

```cron
0 3 * * * cd /app/itambox && python manage.py sync_intune --tenant acme --now
```

The command creates and updates its own Job. Do not schedule
`Command.handle` or call the task directly: the task requires the tenant, actor,
and Job IDs established by the command. Monitor the resulting Job in ITAMbox.

### 1.4  What the sync does

1. **Match** — every Intune device is matched to an `Asset` in ITAMbox by
   `serial_number` (case-insensitive), scoped to the tenant.
2. **Update** — matched assets receive discovery facts in `custom_field_data`:
   `intune_device_id`, `intune_last_sync`, `os_version`.
3. **Create** (when `create_missing: true`) — unmatched devices get a new
   `Manufacturer`, `AssetType`, and `Asset`. The asset's status is set to the
   configured `default_status`.
4. **Holder hint** — if `userPrincipalName` is present, it is recorded as
   `intune_primary_user`; on matched assets, `intune_primary_user_matched` also
   records whether a tenant-local `AssetHolder.upn` matched. This is discovery
   metadata only: it does not create an assignment or check out the asset.
5. **Software** (when `sync_software: true`) — detected apps are upserted into
   `InstalledSoftware` with `discovered_by_agent="Intune"` and a refreshed
   `last_seen_date`, keyed by asset, software, and detected version. Optional
   app discovery/persistence failures can be reported as degraded without
   aborting the device sync.

Results are stored in a `Job` record visible in the Jobs UI.

---

## 2  Generic "push from anything" recipe

Any system with HTTP access can use the REST API, subject to the token owner's
permissions and the tenant boundary. Adapt the pattern for your discovery
source, after mapping its records to ITAMbox IDs and writable fields.

### 2.1  Obtain an API token

In ITAMbox, go to **Profile → API tokens → Add token** and copy the value.

### 2.2  Example script (~40 lines)

```python
"""
Minimal ITAMbox discovery push — adapt for SCCM, Jamf, Lansweeper, etc.
"""
import requests

ITAMBOX_URL   = "https://itambox.example.com"
API_TOKEN     = "your-token-here"
HEADERS = {
    "Authorization": f"Token {API_TOKEN}",
    "Content-Type":  "application/json",
}

def get_or_create_asset(serial: str, name: str, asset_type_id: int, status_id: int) -> dict:
    """Look up an asset by serial; create it if missing (not atomic upsert)."""
    r = requests.get(
        f"{ITAMBOX_URL}/api/assets/assets/",
        params={"serial_number": serial},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    results = r.json()["results"]
    if results:
        return results[0]

    # Resolve these IDs beforehand from the same tenant: asset_type_id and
    # status_id are writable PK fields. `status` is read-only.
    r = requests.post(
        f"{ITAMBOX_URL}/api/assets/assets/",
        json={"name": name, "serial_number": serial,
              "asset_type_id": asset_type_id, "status_id": status_id},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def push_installed_software(asset_id: int, software_id: int, version: str, agent: str):
    """Create an InstalledSoftware record; handle duplicates in the client."""
    r = requests.post(
        f"{ITAMBOX_URL}/api/software/installed-software/",
        json={
            "asset_id":            asset_id,
            "software_id":         software_id,
            "version_detected":    version,
            "discovered_by_agent": agent,
        },
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()

# Example: push one device
asset = get_or_create_asset(serial="SN99999", name="MY-LAPTOP-001",
                            asset_type_id=123, status_id=2)
push_installed_software(
    asset_id=asset["id"],
    software_id=42,   # look up via /api/software/software/?name=...
    version="5.0.1",
    agent="MySCCMScript",
)
```

This template needs tenant-valid IDs and appropriate create/change
permissions. A POST creates a resource; it is not an UPSERT. The client-side
GET-then-POST sequence can race, so handle conflicts and retry/reconcile by
reading the resource again where appropriate. Key endpoints are:

| Endpoint | Purpose |
|----------|---------|
| `GET  /api/assets/assets/?serial_number=…` | Find asset by serial |
| `POST /api/assets/assets/` | Create asset |
| `PATCH /api/assets/assets/{id}/` | Update asset fields |
| `GET  /api/software/software/?name=…` | Find software catalogue entry |
| `POST /api/software/software/` | Create software catalogue entry |
| `POST /api/software/installed-software/` | Create installed software record |
