# Support policy for 1.x

This page states which releases receive fixes, how to report a defect, what to
expect back, and what the project does not provide. ITAMbox is open-source,
self-hosted software. This policy describes commitments the project keeps and
says plainly what it does not.

It is not a service-level agreement. No response time, resolution time, or
availability is promised. Vulnerability reporting has its own channel and rules
in the [security policy](https://github.com/itambox-community/itambox-webapp/blob/main/SECURITY.md), which this page does not
replace.

!!! note "Pre-release status"
    Until `v1.0.0` is published, the security policy governs prerelease support:
    the latest published prerelease is the supported target. This policy takes
    effect with `v1.0.0`.

## What "supported" means

A supported release is one for which a defect report is accepted as a regression
against documented behavior and a fix is considered. Supported does not mean a
fix is scheduled, dated, or guaranteed.

## Which versions receive fixes

- **The current stable minor line receives fixes.** A fix ships as a patch
  release of that line.
- **A previous minor line may receive a time-boxed window for critical fixes.**
  The window is announced when it opens and does not cover regular defects.
- **Fixes on a maintained line ship as patch releases**; the changelog records
  what each patch contains.
- `main` is the development state, not a release, and is not supported for
  production use. Reports against it are welcome as feedback.
- Prereleases (`alpha`, `beta`, `rc`) are superseded by `v1.0.0` and receive no
  fixes once it is published.

## Support by capability grade

The grade of a capability, as described in the
[compatibility policy](compatibility.md) and listed in
[Capability maturity](../operations/capability-maturity.md), decides how a defect
report is treated:

| Grade | Defect reports |
|---|---|
| **Stable** | Accepted as regressions against the documented contract. |
| **Beta** | Accepted. The data model, API shape, or feature set may change between minor releases, so a fix may arrive as a change of shape. |
| **Experimental** | Security fixes only. Functional defects are accepted as feedback, not as regressions. |

No grade waives tenant isolation, authorization, preservation of recorded data,
or secret handling. A defect that lowers one of them is handled as a security
matter whatever the grade.

## Reporting a defect

- **Security vulnerabilities** go through the private channels in the
  [security policy](https://github.com/itambox-community/itambox-webapp/blob/main/SECURITY.md). Do not open a public issue.
- **Other defects** are reported as a [GitHub issue](https://github.com/itambox-community/itambox-webapp/issues/new/choose) using
  the bug report template.
- **Questions and usage help** belong in [GitHub Discussions](https://github.com/itambox-community/itambox-webapp/discussions).
- **Feature requests** use the feature request template. A request is not a
  commitment.

## What to provide

A report that omits these cannot be assessed and may be closed until they are
supplied. Provide:

1. **The exact version**, from `/api/status/`, the login page, or the footer.
2. **The exact artifact**: the container image digest (`docker compose images`
   shows it) or, for a source build, the full Git commit SHA
   (`git rev-parse HEAD`). The version string alone does not identify what runs.
3. **The PostgreSQL major version**, and whether the bundled database service or
   an external one is used.
4. **Reproduction steps** from a known state, and the behavior you expected.
5. **Relevant logs** from the `app` and `worker` services around the failure,
   with secrets, tokens, credentials, and personal data removed.
6. **The relevant configuration**, such as caching, proxy, and SSO settings,
   without secret values.
7. **For an upgrade failure**: the version you upgraded from, the output of
   `manage.py migration_baseline_preflight --format=json`, and the first error
   the migration reported.

Use synthetic or redacted data. Never attach a database dump, a backup, an `.env`
file, an encryption key, or a token to an issue.

## What response to expect

Maintenance is best-effort. Reports are read as capacity allows and answered when
there is something to say.

- A report that meets the requirements above is triaged and labelled. A
  reproducible regression in a Stable surface is prioritized over other work.
- There is no acknowledgement deadline, no resolution deadline, and no escalation
  path with a time attached.
- A report can be declined: out of scope, not reproducible, or against an
  unsupported release. The reason is given.
- Silence on a report means it has not yet been reached, not that it was
  rejected.

## What is community-only

The project does not provide the following. Community members may help each other
in Discussions, without commitment:

- operating, sizing, and tuning your deployment, including reverse proxy, TLS,
  worker, cache, and database topology;
- identity provider setup beyond the documented configuration keys;
- backup tooling, storage, and scheduling, and disaster recovery of the host;
- custom integrations, scripts, and third-party plugins;
- importing or cleaning your own data;
- releases other than the latest line, forks, and modified builds;
- any deployment not following the documented production configuration.

## What this policy does not promise

- No response or fix time, no on-call coverage, no weekend or holiday availability.
- No fix for a version outside the latest line.
- No assistance recovering a deployment that has no verified backup.
- No support for Experimental surfaces beyond security fixes.

## Related

- [Security policy](https://github.com/itambox-community/itambox-webapp/blob/main/SECURITY.md)
- [Compatibility policy](compatibility.md)
- [Upgrade policy](upgrade.md)
