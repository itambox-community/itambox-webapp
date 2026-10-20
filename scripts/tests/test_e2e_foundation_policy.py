"""Static contract tests for the shared Playwright foundation."""

from __future__ import annotations

import csv
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_ROOT = REPO_ROOT / "itambox" / "tests" / "e2e"


class OwnedFoundationFilesTests(unittest.TestCase):
    def test_required_foundation_modules_exist(self):
        required = (
            "auth/admin.setup.ts",
            "auth/operator.setup.ts",
            "auth/viewer.setup.ts",
            "fixtures/test.ts",
            "fixtures/cleanup.ts",
            "fixtures/tenant.ts",
            "fixtures/factories/identity.ts",
            "helpers/api.ts",
            "helpers/errors.ts",
            "helpers/forms.ts",
            "helpers/htmx.ts",
            "helpers/jobs.ts",
            "helpers/names.ts",
            "run-selected.mjs",
        )
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((E2E_ROOT / relative).is_file())

    def test_playwright_config_has_explicit_role_projects_and_no_shared_global_state(self):
        config = (E2E_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
        for project in ("setup-admin", "setup-operator", "setup-viewer", "admin", "operator", "viewer", "anonymous"):
            with self.subTest(project=project):
                self.assertIn("name: '" + project + "'", config)
        self.assertIn("dependencies:", config)
        self.assertIn("setup-admin", config)
        self.assertIn("setup-operator", config)
        self.assertIn("setup-viewer", config)
        self.assertIn("setup-aggregate", config)
        self.assertNotIn("globalSetup", config)
        self.assertNotIn("storageState.json", config)
        self.assertIn("reporter", config)
        self.assertIn("json", config)
        self.assertIn("workers: 1", config)
        self.assertIn("fullyParallel: false", config)

    def test_admin_project_does_not_rediscover_anonymous_or_remote_only_tests(self):
        config = (E2E_ROOT / "playwright.config.ts").read_text(encoding="utf-8")
        admin = config.split("name: 'admin'", 1)[1].split("name: 'operator'", 1)[0]

        self.assertIn("grepInvert: /@(anonymous|non-destructive|operator|viewer)/", admin)

    def test_anonymous_and_remote_smoke_keep_automatic_safety_without_tenant_attestation(self):
        fixture = (E2E_ROOT / "fixtures" / "playwright-fixtures.ts").read_text(encoding="utf-8")
        anonymous = (E2E_ROOT / "spec" / "contracts" / "auth-rbac" / "anonymous-login.spec.ts").read_text(
            encoding="utf-8"
        )
        external = (E2E_ROOT / "spec" / "external" / "oidc-provider.spec.ts").read_text(encoding="utf-8")

        self.assertIn("../../../fixtures/test", anonymous)
        self.assertIn("../../fixtures/test", external)
        self.assertIn("activeTenant: ActiveTenant | null", fixture)
        self.assertIn("['anonymous', 'remote-smoke'].includes(testInfo.project.name)", fixture)
        self.assertIn("testInfo.tags.includes('@aggregate')", fixture)
        self.assertIn("await use(null)", fixture)

    def test_remote_nondestructive_smoke_precedes_destructive_shared_target_block(self):
        tenant = (E2E_ROOT / "fixtures" / "tenant.ts").read_text(encoding="utf-8")
        remote = "testInfo.tags.includes('@non-destructive') && testInfo.project.name === 'remote-smoke'"

        self.assertLess(tenant.index(remote), tenant.index("SHARED_TARGETS.has(host)"))
        self.assertNotIn("E2E_INSTANCE_ATTESTATION", tenant)

    def test_automatic_fixture_contracts_are_fail_closed(self):
        fixture = (E2E_ROOT / "fixtures" / "test.ts").read_text(encoding="utf-8") + (
            E2E_ROOT / "fixtures" / "playwright-fixtures.ts"
        ).read_text(encoding="utf-8")
        cleanup = (E2E_ROOT / "fixtures" / "cleanup.ts").read_text(encoding="utf-8")
        tenant = (E2E_ROOT / "fixtures" / "tenant.ts").read_text(encoding="utf-8")
        errors = (E2E_ROOT / "helpers" / "errors.ts").read_text(encoding="utf-8")
        names = (E2E_ROOT / "helpers" / "names.ts").read_text(encoding="utf-8")

        self.assertIn("{ auto: true }", fixture)
        # The attestation is rendered once per shell in layout.html; the desktop
        # and mobile headers share one switcher include (#929) and must not
        # duplicate it, because the E2E fixtures require exactly one hook.
        shell = (REPO_ROOT / "itambox" / "templates" / "layout.html").read_text(encoding="utf-8")
        self.assertIn("data-testid", shell)
        self.assertIn("data-tenant-id", shell)
        self.assertIn("data-tenant-slug", shell)
        switcher = (REPO_ROOT / "itambox" / "templates" / "global_includes" / "_workspace_switcher.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("data-testid", switcher)
        for shell_path in ("global_includes/_topbar.html", "layout.html"):
            rendered = (REPO_ROOT / "itambox" / "templates" / shell_path).read_text(encoding="utf-8")
            self.assertIn("global_includes/_workspace_switcher.html", rendered)
        self.assertIn("assertSafeTarget", fixture)
        self.assertIn("activeTenant", fixture)
        self.assertIn("api: APIRequestContext", fixture)
        self.assertIn("E2E_API_TOKEN", fixture)
        self.assertIn("Authorization: `Token ${token}`", fixture)
        self.assertIn("E2E_TENANT_SLUG", tenant)
        self.assertIn("console", errors)
        self.assertIn("pageerror", errors)
        self.assertIn("status() >= 500", errors)
        self.assertIn("reverse()", cleanup)
        self.assertIn("failures", cleanup)
        for token in ("project.name", "workerIndex", "retry", "runId"):
            with self.subTest(token=token):
                self.assertIn(token, names)

    def test_owned_rest_cleanup_uses_the_etag_aware_helper(self):
        helper = (E2E_ROOT / "helpers" / "api.ts").read_text(encoding="utf-8")
        self.assertIn("export async function deleteOwnedResource", helper)
        self.assertIn("current.headers()['etag']", helper)
        self.assertIn("'If-Match': etag", helper)
        self.assertIn("expectedStatus", helper)

        sources = list((E2E_ROOT / "fixtures" / "factories").rglob("*.ts"))
        sources.extend(path for path in (E2E_ROOT / "spec" / "apps").rglob("*.ts") if path.name != "sso-scim.spec.ts")
        contents = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotIn(".delete(", contents)

    def test_static_integration_examples_run_below_playwright(self):
        legacy_spec = E2E_ROOT / "spec" / "legacy-smoke" / "rest-guides.spec.ts"
        integration = REPO_ROOT / "itambox" / "docs" / "integration"
        developer_guide = integration / "developer_guide.md"
        bulk_guide = integration / "bulk_import_guide.md"
        bulk_csv = integration / "bulk_assets.csv"

        self.assertFalse(legacy_spec.exists())
        for document in (developer_guide, bulk_guide):
            content = document.read_text(encoding="utf-8")
            self.assertGreater(len(content), 100)
            self.assertTrue(content.startswith("#"))

        rows = list(csv.reader(bulk_csv.read_text(encoding="utf-8").splitlines()))
        self.assertGreater(len(rows), 1)
        headers = rows[0]
        self.assertTrue(all(len(row) == len(headers) for row in rows[1:]))
        for field in ("name", "asset_tag", "serial_number", "description"):
            self.assertIn(field, headers)

    def test_contract_scopes_have_real_behavioral_evidence(self):
        required_markers = {
            "generic-object/generic-object.spec.ts": ("manufacturer create response", "updated manufacturer readback"),
            "tenant-isolation/tenant-shell.spec.ts": ("foreign tenant", "@operator"),
            "auth-rbac/role-boundary.spec.ts": ("viewer asset create boundary", "403"),
            "auth-rbac/anonymous-login.spec.ts": ("maxRedirects: 0", "anonymous dashboard redirect"),
            "asset-custody/asset-lifecycle.spec.ts": ("asset-checkout-action", "asset-checkin-action"),
            "cross-app/asset-inventory.spec.ts": ("quick-add", "cleanupAllocation"),
            "jobs/asset-scanner.spec.ts": ("Cancel job", "Bulk Check-in: 2 Assets"),
            "soft-delete/live-assets.spec.ts": ("Restore", "Delete Permanently"),
        }
        contracts = E2E_ROOT / "spec" / "contracts"
        for relative, markers in required_markers.items():
            with self.subTest(contract=relative):
                source = (contracts / relative).read_text(encoding="utf-8")
                for marker in markers:
                    self.assertIn(marker, source)
                self.assertNotIn("openOwnedSurface", source)

    def test_reference_app_scopes_do_not_retain_surface_only_placeholders(self):
        for app in (
            "assets",
            "compliance",
            "core",
            "extras",
            "inventory",
            "itambox",
            "licenses",
            "organization",
            "procurement",
            "software",
            "subscriptions",
            "users",
        ):
            with self.subTest(app=app):
                surfaces = sorted((E2E_ROOT / "spec" / "apps" / app).glob("*-surface.spec.ts"))
                self.assertEqual(surfaces, [])

    def test_new_owned_specs_do_not_use_known_fail_open_shortcuts(self):
        owned = E2E_ROOT / "spec" / "apps"
        self.assertTrue(owned.is_dir())
        contents = "\n".join(path.read_text(encoding="utf-8") for path in owned.rglob("*.ts"))
        self.assertNotIn("waitForTimeout(", contents)
        self.assertNotIn("console.log(", contents)
        self.assertNotIn("console.error(", contents)
        self.assertNotIn("test.skip(", contents)
        self.assertNotIn("test.fixme(", contents)
        self.assertNotIn("test.setTimeout(", contents)
        self.assertNotRegex(contents, r"if\s*\(\s*\(await\s+[^\n]+\.count\(\)\)")


if __name__ == "__main__":
    unittest.main()
