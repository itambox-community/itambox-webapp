# Resource grant expiry runbook

This runbook covers the scheduled expiry sweep. For the feature itself (what a
resource grant is, who may grant and revoke, the access levels, and how a
grantee uses a shared stock pool), see
[Resource Grants](../usage/resource-grants.md).

The expiry coordinator runs hourly and creates one owner-tenant run per UTC
hour. Confirm the django-q2 schedule named **Hourly Resource Grant Expiry
Sweep** and check worker health before investigating a tenant run.

Runs are read-only operational records. `queued` and `running` are in flight;
`retryable` and `enqueue_failed` are safe to wait for or allow the coordinator
to repair; `success` and `skipped` are normal; `partial` means safe rows were
revoked while invalid rows need review; `terminal` requires operator action.
Stable codes include `resource_grant_expiry_succeeded`,
`resource_grant_expiry_no_due`, `resource_grant_expiry_partial`,
`resource_grant_expiry_db_retry`, `resource_grant_expiry_enqueue_failed`,
`resource_grant_expiry_invalid_grant`,
`resource_grant_expiry_tenant_unresolvable`, and
`resource_grant_expiry_retry_exhausted`. Error messages are deliberately
redacted; use only tenant ID, run ID, code, counts, and timestamps in alerts.

From a run detail page, follow an evidence row to the GET-only resource-grant
audit detail. The audit API is container-scoped: owners, direct grantees, and
tenants in a covered live group subtree can see a grant, but only the owner
tenant sees the expiry-run aggregation. A token is pinned to its tenant. An
unbound platform superuser is global; a tenant- or group-bound superuser is
still limited to that selected container.

If a tenant cannot be resolved, do not retry it from another tenant context and
do not recreate its run. Resolve the stable tenant/run error through the normal
tenant lifecycle and let a later hourly repair establish the boundary again.

## Correcting a deadline and restoring one grant

Use the named command with explicit confirmation and exactly one deadline
choice:

```text
python manage.py restore_resource_grant --grant GRANT_ID --tenant TENANT_ID --user USER_ID --clear-deadline --confirm
python manage.py restore_resource_grant --grant GRANT_ID --tenant TENANT_ID --user USER_ID --valid-until 2030-01-01T12:00:00+00:00 --confirm
```

The service locks the grant, verifies owner permission, corrects or clears the
deadline before restoring it, and performs one validated model save. Verify
the operator's RBAC in the owner or active grantee tenant before expecting
access to return: a grant never supplies user permission. If an active
replacement grant conflicts with restoration, resolve that single-row
conflict; do not delete or bulk-restore records to force a result.

`QuerySet.update()`, bulk restore, direct SQL, and REST restore/revoke actions
are unsupported. The prior expiry `ObjectChange`, run, and evidence remain
historical evidence. Reversing or dropping the nullable field does not undo a
committed soft revocation.

Runs and evidence follow changelog retention. Only terminal runs are pruned,
using `finished_at`; queued, running, retryable-queued, and enqueue-failed
runs are preserved. Evidence is removed before its run, while the linked
`ObjectChange` follows its own normal retention. A retained evidence row whose
change link was pruned is expected to serialize as `kind=unknown` and remains
safe to audit. Tenant overrides, the global fallback, and zero/legal-hold
semantics are the existing changelog policy; no new setting is introduced.
