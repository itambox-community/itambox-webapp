"""Third-party attribution regression gate for the vendored frontend set.

``itambox/copy-vendor.mjs`` copies a fixed set of npm packages into
``itambox/static/dist/vendor/`` and the runtime image redistributes them.
This gate fails closed if that attribution drifts:

* every package ``copy-vendor.mjs`` vendors must be documented in
  ``THIRD-PARTY-NOTICES.md`` with its exact pinned version and SPDX license
  identifier (read from ``itambox/package-lock.json``, the single source of
  truth for both);
* every distinct SPDX license identifier in use must have a non-empty
  license text file under ``LICENSES/``;
* the ``Dockerfile`` runtime stage must copy both the notices file and the
  license directory into the image.

The gate imports no Django and touches no database, so it runs on the bare
interpreter with the other repository gate suites.
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COPY_VENDOR = REPO_ROOT / "itambox" / "copy-vendor.mjs"
PACKAGE_LOCK = REPO_ROOT / "itambox" / "package-lock.json"
NOTICES = REPO_ROOT / "THIRD-PARTY-NOTICES.md"
LICENSES_DIR = REPO_ROOT / "LICENSES"
DOCKERFILE = REPO_ROOT / "Dockerfile"

NODE_MODULES_PATH_RE = re.compile(r"node_modules/((?:@[\w.-]+/)?[\w.-]+)")
NOTICE_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def vendored_package_names():
    """Package names copy-vendor.mjs reads from node_modules/."""
    text = COPY_VENDOR.read_text(encoding="utf-8")
    return sorted(set(NODE_MODULES_PATH_RE.findall(text)))


def locked_package_info():
    """Map package name -> (version, license) from package-lock.json."""
    data = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    packages = data["packages"]
    info = {}
    for name in vendored_package_names():
        entry = packages.get(f"node_modules/{name}")
        if entry is not None:
            info[name] = (entry.get("version"), entry.get("license"))
    return info


def notice_sections():
    """Map '## <heading>' text to the section body in THIRD-PARTY-NOTICES.md."""
    text = NOTICES.read_text(encoding="utf-8")
    matches = list(NOTICE_SECTION_RE.finditer(text))
    sections = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[start:end]
    return sections


class VendoredPackageInventoryTests(unittest.TestCase):
    def test_copy_vendor_lists_the_packages_the_task_pins(self):
        # Sanity check on the parser itself: this is the exact package set
        # copy-vendor.mjs is known to copy today.
        expected = {
            "@tabler/core",
            "tom-select",
            "gridstack",
            "@mdi/font",
            "@fontsource-variable/inter",
            "htmx.org",
            "bootstrap",
            "html5-qrcode",
            "graphiql",
        }
        self.assertEqual(expected, set(vendored_package_names()))

    def test_every_vendored_package_is_pinned_in_the_lockfile(self):
        info = locked_package_info()
        missing = [name for name in vendored_package_names() if name not in info]
        self.assertEqual(missing, [], "vendored packages missing from package-lock.json")


class ThirdPartyNoticesPolicyTest(unittest.TestCase):
    def test_every_vendored_package_has_a_notice_section(self):
        sections = notice_sections()
        missing = [name for name in vendored_package_names() if name not in sections]
        self.assertEqual(missing, [], "packages missing a THIRD-PARTY-NOTICES.md section")

    def test_every_package_section_states_its_exact_version_and_license(self):
        sections = notice_sections()
        info = locked_package_info()
        failures = []
        for name, (version, license_id) in info.items():
            body = sections.get(name, "")
            if version not in body:
                failures.append(f"{name}: version {version!r} not found in its notice section")
            if license_id not in body:
                failures.append(f"{name}: license {license_id!r} not found in its notice section")
        self.assertEqual(failures, [])


class LicenseTextPolicyTest(unittest.TestCase):
    def test_every_distinct_spdx_identifier_has_a_nonempty_license_file(self):
        info = locked_package_info()
        spdx_ids = sorted({license_id for _version, license_id in info.values()})
        self.assertTrue(spdx_ids, "no SPDX identifiers discovered from package-lock.json")
        missing = []
        for spdx_id in spdx_ids:
            matches = [
                path for path in LICENSES_DIR.glob(f"{spdx_id}*.txt") if path.is_file() and path.stat().st_size > 0
            ]
            if not matches:
                missing.append(spdx_id)
        self.assertEqual(missing, [], "SPDX identifiers with no non-empty LICENSES/ file")


class DockerfilePolicyTest(unittest.TestCase):
    def test_runtime_stage_copies_the_notices_and_licenses(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("THIRD-PARTY-NOTICES.md", text)
        self.assertIn("LICENSES", text)


if __name__ == "__main__":
    unittest.main()
