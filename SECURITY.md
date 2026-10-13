# Security Policy

ITAMbox is an open-source, self-hosted IT asset management application and is currently in **public beta**. This policy explains which code is covered by security reports, how to report a vulnerability privately, and how coordinated disclosure works. It describes real commitments only and does not establish a service-level agreement.

---

## Supported versions

ITAMbox publishes tagged prereleases on GitHub (`1.0.0-alpha.N`, `1.0.0-beta.N`, and later `1.0.0-rc.N`). The **latest published prerelease is the current supported target**; older prereleases are superseded by it and do not receive dedicated fixes.

| Target | Status |
|---|---|
| Latest published prerelease | Current supported target for security reports |
| `main` | Development state, not a published release; may contain unreleased or unfinished changes |
| Older prereleases | Superseded by the latest prerelease; no backport guarantee |
| Stable `1.0.x` releases | Not available yet; a support policy will accompany the first stable release |

Reports against the latest prerelease and against current `main` are both welcome. Reports about superseded prereleases are acknowledged, though any fix targets the supported line. Capability maturity grades — Stable, Beta, and Experimental in the [capability maturity guide](itambox/docs/operations/capability-maturity.md) — describe compatibility expectations only. They never waive tenant isolation or any other security requirement.

---

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use one of the private channels:

1. **GitHub Private Vulnerability Reporting** — open the repository's [Security tab](https://github.com/itambox-community/itambox-webapp/security) and select *Report a vulnerability*. This opens a private advisory visible only to you and the maintainers.
2. **Email** — [security@itambox.dev](mailto:security@itambox.dev) with the subject prefix `[ITAMbox Security]`.

A useful report describes:

- the affected release tag or commit SHA;
- reproduction steps or a proof of concept;
- the impact you can demonstrate;
- which tenant or security boundary is affected;
- the relevant configuration (for example caching, proxy, or SSO settings);
- whether exploitation requires authentication or elevated scope.

Please keep reports free of real tenant data, customer secrets, credentials, and personal information; synthetic or redacted examples are sufficient.

---

## Coordinated disclosure

Report privately and give maintainers a reasonable opportunity to assess and fix the issue before disclosing it publicly. Where a coordinated release is feasible, maintainers and the reporter agree on disclosure timing once the issue and remediation path are understood. There is no fixed embargo period and no expectation that a report stays private indefinitely; if you intend to publish, tell us so a disclosure date can be agreed together.

---

## Operator context

Deployment hardening is the operator's responsibility and is documented separately: secret and key management, TLS termination, proxy and rate-limit configuration, backup protection, and shared-cache requirements live in the [deployment security guide](itambox/docs/security/deployment-security.md) and the [installation guide](itambox/docs/operations/installation.md). Findings are assessed against that documented deployment model.

Maintainers assess reproducibility, impact, and next steps as availability permits. Response and remediation times are not guaranteed during the prerelease period.
