import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.release_policy import (
    ReleasePolicyError,
    extract_release_notes,
    main,
    parse_version,
    validate_repository,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class VersionPolicyTests(unittest.TestCase):
    def test_dotted_prereleases_parse_and_sort_in_promotion_order(self):
        values = [
            "1.0.0-alpha.2",
            "1.0.0",
            "1.0.0-rc.1",
            "1.0.0-beta.1",
            "1.0.0-alpha.1",
            "1.0.0-alpha.10",
        ]

        parsed = [parse_version(value) for value in values]

        self.assertEqual(
            [item.semver for item in sorted(parsed)],
            [
                "1.0.0-alpha.1",
                "1.0.0-alpha.2",
                "1.0.0-alpha.10",
                "1.0.0-beta.1",
                "1.0.0-rc.1",
                "1.0.0",
            ],
        )
        self.assertEqual(parse_version("1.0.0-alpha.1").pep440, "1.0.0a1")
        self.assertEqual(parse_version("1.0.0-beta.2").pep440, "1.0.0b2")
        self.assertEqual(parse_version("1.0.0-rc.3").pep440, "1.0.0rc3")
        self.assertEqual(parse_version("1.0.0").pep440, "1.0.0")

    def test_non_dotted_or_non_numeric_prereleases_are_rejected(self):
        invalid = [
            "1.0.0-alpha1",
            "1.0.0-alpha",
            "1.0.0-alpha.x",
            "1.0.0-preview.1",
            "v1.0.0-alpha.1",
            "1.0",
        ]

        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ReleasePolicyError):
                parse_version(value)


class RepositoryPolicyTests(unittest.TestCase):
    def test_checked_in_release_metadata_is_consistent(self):
        version = validate_repository(REPOSITORY_ROOT, expected_version="1.0.0-beta.6")

        self.assertEqual(version.semver, "1.0.0-beta.6")
        self.assertEqual(version.pep440, "1.0.0b6")

    def test_checked_in_openapi_schema_matches_release_identity(self):
        version = validate_repository(REPOSITORY_ROOT)
        schema = (REPOSITORY_ROOT / "itambox" / "schema.yaml").read_text(encoding="utf-8")
        match = re.search(r"(?m)^  version: (?P<version>\S+)$", schema)

        self.assertIsNotNone(match)
        self.assertEqual(match.group("version"), version.semver)

    def _write_fixture(
        self,
        root: Path,
        *,
        project_version: str = "1.0.0-alpha.1",
        source_version: str = "1.0.0-alpha.1",
        locked_version: str = "1.0.0a1",
        changelog_version: str = "1.0.0-alpha.1",
        readme_version: str = "1.0.0-alpha.1",
    ) -> None:
        (root / "itambox" / "itambox").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "itambox"\nversion = {project_version!r}\n',
            encoding="utf-8",
        )
        (root / "itambox" / "itambox" / "release.py").write_text(f"VERSION = {source_version!r}\n", encoding="utf-8")
        (root / "uv.lock").write_text(
            f'[[package]]\nname = "itambox"\nversion = {locked_version!r}\nsource = {{ virtual = "." }}\n',
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            f"## [{changelog_version}] - 2026-07-24\n\n"
            "### Added\n\n- First alpha release.\n\n"
            f"[{changelog_version}]: https://github.com/itambox-community/itambox-webapp/releases/tag/v{changelog_version}\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text(
            f"This repository is pre-release. `{readme_version}` is current version metadata.\n",
            encoding="utf-8",
        )

    def test_repository_metadata_maps_to_one_release_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root)

            result = validate_repository(root, expected_version="1.0.0-alpha.1")

            self.assertEqual(result.semver, "1.0.0-alpha.1")
            self.assertEqual(result.pep440, "1.0.0a1")
            self.assertEqual(
                extract_release_notes(root / "CHANGELOG.md", result.semver),
                "### Added\n\n- First alpha release.",
            )

    def test_metadata_drift_is_rejected_with_the_source_named(self):
        cases = [
            ("project_version", "1.0.0-alpha.2", "pyproject.toml"),
            ("source_version", "1.0.0-alpha.2", "release.py"),
            ("locked_version", "1.0.0a2", "uv.lock"),
            ("changelog_version", "1.0.0-alpha.2", "CHANGELOG.md"),
            ("readme_version", "1.0.0-alpha.2", "README.md"),
        ]
        for field, value, expected_source in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._write_fixture(root, **{field: value})

                with self.assertRaisesRegex(ReleasePolicyError, expected_source):
                    validate_repository(root, expected_version="1.0.0-alpha.1")

    def test_cli_verifies_metadata_and_prints_release_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root)
            output = io.StringIO()

            with redirect_stdout(output):
                status = main(
                    [
                        "verify",
                        "--root",
                        str(root),
                        "--version",
                        "1.0.0-alpha.1",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertIn("release metadata valid: 1.0.0-alpha.1", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        "notes",
                        "--root",
                        str(root),
                        "--version",
                        "1.0.0-alpha.1",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().strip(), "### Added\n\n- First alpha release.")

    def test_missing_or_empty_release_notes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "CHANGELOG.md"
            path.write_text(
                "# Changelog\n\n## [1.0.0-alpha.1] - 2026-07-24\n\n",
                encoding="utf-8",
            )

            with self.assertRaises(ReleasePolicyError):
                extract_release_notes(path, "1.0.0-alpha.1")


class ReleaseAutomationContractTests(unittest.TestCase):
    def test_runtime_image_declares_required_oci_identity_labels(self):
        dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")

        for build_arg in ("ITAMBOX_VERSION", "ITAMBOX_REVISION", "ITAMBOX_SOURCE"):
            self.assertIn(f"ARG {build_arg}", dockerfile)
        for label in (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
        ):
            self.assertIn(label, dockerfile)

    def test_runtime_image_explicitly_installs_ca_certificates(self):
        dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        runtime_stage = re.split(r"FROM python:3\.12-slim-bookworm@sha256:[0-9a-fA-F]{64}", dockerfile)[-1]
        install_block = runtime_stage.split("RUN apt-get update", 1)[1].split("&& rm -rf /var/lib/apt/lists/*", 1)[0]

        self.assertIn("ca-certificates", install_block)

    def test_runtime_image_explicitly_installs_openssl_and_tzdata(self):
        dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        runtime_stage = re.split(r"FROM python:3\.12-slim-bookworm@sha256:[0-9a-fA-F]{64}", dockerfile)[-1]
        install_block = runtime_stage.split("RUN apt-get update", 1)[1].split("&& rm -rf /var/lib/apt/lists/*", 1)[0]

        for package in ("openssl", "libssl3", "tzdata"):
            self.assertIn(package, install_block)

    def test_release_image_gates_block_every_unsuppressed_finding(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertEqual(workflow.count("--fail-on any"), 2)
        rehearsal = workflow[: workflow.index("prepare-release:")]
        prepare_release = workflow[workflow.index("prepare-release:") :]
        self.assertIn("--fail-on any", rehearsal)
        self.assertIn("--fail-on any", prepare_release)

    def test_public_guidance_uses_dotted_prerelease_examples(self):
        security_policy = (REPOSITORY_ROOT / "SECURITY.md").read_text(encoding="utf-8")
        bug_template = (REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text(encoding="utf-8")

        self.assertNotIn("1.0.0-alpha1", security_policy)
        self.assertNotIn("1.0.0-alpha1", bug_template)
        self.assertIn("1.0.0-alpha.1", bug_template)

    def test_release_workflow_separates_pr_rehearsal_from_draft_preparation(self):
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn('- "README.md"', workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("github.event_name == 'pull_request'", workflow)
        self.assertIn("validate-dispatch-ref:", workflow)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/main"', workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("commits/${GITHUB_SHA}/pulls", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertNotIn("git ls-remote --exit-code --tags origin", workflow)
        self.assertIn('git push --porcelain origin "$GITHUB_SHA:refs/tags/v${RELEASE_VERSION}"', workflow)
        self.assertIn("verify_release_tag()", workflow)
        self.assertGreaterEqual(workflow.count("verify_release_tag"), 3)
        self.assertIn("Release tag does not resolve to the reviewed commit", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--verify-tag", workflow)
        self.assertIn('--target "$GITHUB_SHA"', workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("--prerelease", workflow)
        self.assertIn("docker image inspect", workflow)
        self.assertNotIn("push: true", workflow)
        self.assertNotIn("self-hosted", workflow)


class ReleaseWorkflowHardeningTests(unittest.TestCase):
    """Security invariants of the release workflow itself.

    Each check rejects a concrete unsafe configuration: a floating action tag,
    an over-broad permission, an SBOM that is not generated from the released
    image, a release asset list that silently drops the SBOM, an attestation
    that is not bound to the reviewed run, or a registry publication that could
    drift from the qualified image. The workflow is the artifact here, so the
    invariants are modelled over its parsed job structure.
    """

    WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
    JOB_HEADER = re.compile(r"(?m)^  (?P<name>[a-z0-9_-]+):\s*$")

    def setUp(self):
        self.workflow_text = self.WORKFLOW_PATH.read_text(encoding="utf-8")
        self.jobs = self._job_blocks()

    def _job_blocks(self) -> dict[str, str]:
        jobs_section = self.workflow_text.split("\njobs:\n", 1)[1]
        matches = list(self.JOB_HEADER.finditer(jobs_section))
        blocks = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(jobs_section)
            blocks[match.group("name")] = jobs_section[match.start() : end]
        return blocks

    def test_every_action_use_is_pinned_to_a_full_commit_sha(self):
        uses = re.findall(r"(?m)^[ \t]*(?:-[ \t]+)?uses:[ \t]*(\S+)", self.workflow_text)

        self.assertGreaterEqual(len(uses), 5)
        for use in uses:
            if use.startswith("./"):
                continue  # local reusable workflow, not a third-party action
            path, separator, ref = use.partition("@")
            with self.subTest(use=use):
                self.assertTrue(path and separator, f"{use} is not pinned")
                self.assertRegex(ref, r"^[0-9a-f]{40}$")

    def test_rehearsal_job_cannot_attest_publish_or_mutate_packages(self):
        rehearsal = self.jobs["rehearsal"]

        self.assertIn("contents: read", rehearsal)
        for forbidden in (
            "id-token",
            "attestations",
            "packages:",
            "artifact-metadata",
            "docker login",
            "docker push",
            "actions/attest",
            "push-to-registry",
            "gh attestation verify",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rehearsal)
        # The rehearsal compiles both release platforms into the builder cache
        # and loads the host-platform image for scanning only; publishing stays
        # impossible because nothing is exported to a registry.
        self.assertIn("--output type=cacheonly", rehearsal)
        self.assertIn("--load", rehearsal)
        self.assertNotIn("--push", rehearsal)
        self.assertNotIn("push-by-digest", rehearsal)

    def test_attestation_scopes_are_limited_to_the_prepare_job(self):
        prepare = self.jobs["prepare-release"]

        self.assertIn("id-token: write", prepare)
        self.assertIn("attestations: write", prepare)
        top_level = self.workflow_text.split("\njobs:\n", 1)[0]
        self.assertIn("permissions:\n  contents: read", top_level)
        self.assertNotIn("id-token", top_level)
        self.assertNotIn("attestations", top_level)

    def test_package_publication_scopes_are_limited_to_the_prepare_job(self):
        prepare = self.jobs["prepare-release"]

        self.assertIn("packages: write", prepare)
        rehearsal = self.jobs["rehearsal"]
        self.assertNotIn("packages: write", rehearsal)
        top_level = self.workflow_text.split("\njobs:\n", 1)[0]
        self.assertNotIn("packages", top_level)

    def test_registry_storage_records_are_disabled_without_the_scope(self):
        prepare = self.jobs["prepare-release"]

        # Storage records require an organization-owned repository; the
        # itambox account is a user account, so the scope must not be
        # requested and both OCI attestations disable records explicitly.
        self.assertNotIn("artifact-metadata", self.workflow_text)
        for step_name in (
            "Attest the published image provenance",
            "Attest the published image SBOM",
        ):
            with self.subTest(step=step_name):
                step = prepare.split(step_name, 1)[1].split("- name:", 1)[0]
                self.assertIn("create-storage-record: false", step)
                self.assertIn("push-to-registry: true", step)

    def test_sbom_is_generated_from_the_image_that_is_released(self):
        rehearsal = self.jobs["rehearsal"]
        prepare = self.jobs["prepare-release"]

        self.assertIn("--format spdx-json", rehearsal)
        self.assertIn("scripts/release_sbom.py verify", rehearsal)
        self.assertIn("itambox:release-rehearsal", rehearsal)
        self.assertIn("--format spdx-json", prepare)
        self.assertIn("scripts/release_sbom.py verify", prepare)
        self.assertIn('"itambox:${RELEASE_VERSION}"', prepare)

    def test_sbom_is_shipped_as_a_release_asset_with_the_release_identity(self):
        self.assertIn("itambox-${{ inputs.version }}.sbom.spdx.json", self.workflow_text)
        release_command = self.workflow_text.split('gh release create "$release_tag"', 1)[1]
        for asset in (
            '"itambox-${RELEASE_VERSION}.tar.gz"',
            '"itambox-${RELEASE_VERSION}.tar.gz.sha256"',
            '"itambox-${RELEASE_VERSION}.sbom.spdx.json"',
        ):
            with self.subTest(asset=asset):
                self.assertIn(asset, release_command)

    def test_release_attestation_is_bound_to_the_reviewed_run_and_verified(self):
        prepare = self.jobs["prepare-release"]

        self.assertIn("actions/attest@", prepare)
        self.assertNotIn("attest-build-provenance", self.workflow_text)
        self.assertIn("gh attestation verify", prepare)
        self.assertIn('"${GITHUB_REPOSITORY}/.github/workflows/release.yml"', prepare)
        self.assertIn('--source-ref "refs/heads/main"', prepare)
        self.assertIn('--source-digest "$GITHUB_SHA"', prepare)

    def test_published_tag_promotes_the_digest_the_qualified_build_pushed(self):
        prepare = self.jobs["prepare-release"]

        # The registry image is the exact image that was built, scanned,
        # archived, and SBOM-validated: one multi-platform build pushes its
        # result by digest while the gates are pending, and the version tag is
        # created from that recorded digest afterwards. No rebuild, no classic
        # ``docker push``, and no local re-upload of the release image.
        self.assertEqual(len(re.findall(r"(?m)^\s*docker buildx build \\$", prepare)), 1)
        self.assertEqual(len(re.findall(r"(?m)^\s*docker build \\$", prepare)), 0)
        self.assertNotIn("docker push", prepare)
        self.assertNotIn("--push ", prepare)
        self.assertNotIn("docker/build-push-action", self.workflow_text)
        self.assertIn("--platform linux/amd64,linux/arm64", prepare)
        self.assertIn(
            '--output "type=image,name=${REGISTRY}/${IMAGE_NAME},push-by-digest=true,push=true"',
            prepare,
        )
        self.assertIn("--metadata-file itambox-build-metadata.json", prepare)
        self.assertIn(
            'docker pull --platform linux/amd64 "${REGISTRY}/${IMAGE_NAME}@${index_digest}"',
            prepare,
        )
        self.assertIn(
            'docker tag "${REGISTRY}/${IMAGE_NAME}@${index_digest}" "itambox:${RELEASE_VERSION}"',
            prepare,
        )
        self.assertIn(
            'docker buildx imagetools create -t "${REGISTRY}/${IMAGE_NAME}:${RELEASE_VERSION}"',
            prepare,
        )
        self.assertIn('"${REGISTRY}/${IMAGE_NAME}@${INDEX_DIGEST}"', prepare)
        self.assertIn("scripts/check_registry_image.py verify-manifest", prepare)
        self.assertIn("scripts/check_registry_image.py verify-image", prepare)
        self.assertIn("docker buildx imagetools inspect --raw", prepare)
        self.assertIn('--raw "${REGISTRY}/${IMAGE_NAME}@${image_manifest_digest}"', prepare)
        self.assertIn("GHCR_MANIFEST_DIGEST", prepare)
        self.assertIn("IMAGE_MANIFEST_DIGEST_AMD64", prepare)
        self.assertIn("IMAGE_MANIFEST_DIGEST_ARM64", prepare)
        self.assertIn("IMAGE_CONFIG_DIGEST_AMD64", prepare)
        self.assertIn("IMAGE_CONFIG_DIGEST_ARM64", prepare)
        # The build must be logged in before it pushes its result, and the tag
        # may only be created after every gate and the SBOM validation passed.
        self.assertLess(
            prepare.index("Log in to the GitHub Container Registry"),
            prepare.index("Build and push the reviewed release image by digest"),
        )
        self.assertLess(
            prepare.index("Validate release image SBOM"),
            prepare.index("Promote the verified image digest to the release tag"),
        )
        self.assertLess(
            prepare.index("Promote the verified image digest to the release tag"),
            prepare.index("Prepare draft GitHub release"),
        )

    def test_both_release_jobs_prepare_amd64_and_arm64_builds(self):
        rehearsal = self.jobs["rehearsal"]
        prepare = self.jobs["prepare-release"]

        # The rehearsal compiles both platforms into the builder cache and
        # loads the host-platform image for the scan chain; the release build
        # pushes the multi-platform index by digest without a local load.
        self.assertIn("--platform linux/amd64,linux/arm64", rehearsal)
        self.assertIn("--output type=cacheonly", rehearsal)
        self.assertIn("--platform linux/amd64", rehearsal)
        self.assertIn("--load", rehearsal)
        self.assertNotIn("--push ", rehearsal)
        self.assertIn("--platform linux/amd64,linux/arm64", prepare)
        self.assertIn("push-by-digest=true", prepare)
        for job_name in ("rehearsal", "prepare-release"):
            with self.subTest(job=job_name):
                self.assertIn("docker/setup-qemu-action@", self.jobs[job_name])
                self.assertIn("docker/setup-buildx-action@", self.jobs[job_name])

    def test_both_platform_images_are_boot_checked_before_the_draft_release(self):
        prepare = self.jobs["prepare-release"]

        self.assertIn("Boot-check both platform images", prepare)
        boot_step = prepare.split("Boot-check both platform images", 1)[1].split("- name:", 1)[0]
        self.assertIn('--platform "linux/${platform}"', boot_step)
        self.assertIn("python manage.py check", boot_step)
        self.assertIn("IMAGE_MANIFEST_DIGEST_${upper}", boot_step)
        self.assertIn("ITAMBOX_SECRET_KEY", boot_step)
        self.assertIn("ITAMBOX_DB_PASSWORD", boot_step)
        self.assertLess(
            prepare.index("Boot-check both platform images"),
            prepare.index("Prepare draft GitHub release"),
        )

    def test_registry_identity_matches_the_repository_and_publishes_no_moving_alias(self):
        prepare = self.jobs["prepare-release"]

        self.assertIn("REGISTRY: ghcr.io", prepare)
        self.assertIn("IMAGE_NAME: ${{ github.repository }}", prepare)
        self.assertNotIn(":latest", prepare)
        self.assertNotIn(":beta", prepare)

    def test_registry_attestations_share_the_registry_digest_and_are_verified_before_the_release(self):
        prepare = self.jobs["prepare-release"]

        self.assertEqual(prepare.count("subject-name: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}"), 2)
        self.assertEqual(prepare.count("subject-digest: ${{ steps.ghcr-image.outputs.GHCR_MANIFEST_DIGEST }}"), 2)
        self.assertEqual(prepare.count("push-to-registry: true"), 2)
        self.assertIn("sbom-path: itambox-${{ inputs.version }}.sbom.spdx.json", prepare)
        self.assertIn("oci://${REGISTRY}/${IMAGE_NAME}@${GHCR_MANIFEST_DIGEST}", prepare)
        self.assertIn('--predicate-type "$predicate"', prepare)
        self.assertIn('"https://slsa.dev/provenance/v1"', prepare)
        self.assertIn('"https://spdx.dev/Document/v2.3"', prepare)
        self.assertIn("--bundle-from-oci", prepare)
        self.assertLess(
            prepare.index("Resolve and verify the published registry image identity"),
            prepare.index("Prepare draft GitHub release"),
        )
        self.assertLess(
            prepare.index("Verify the published image attestations"),
            prepare.index("Prepare draft GitHub release"),
        )

    def test_all_published_file_subjects_are_verified(self):
        prepare = self.jobs["prepare-release"]
        verification = prepare.split("Verify release artifact attestation", 1)[1]
        verification = verification.split("Retain reviewed image candidate", 1)[0]

        for artifact in (
            "itambox-${RELEASE_VERSION}.tar.gz",
            "itambox-${RELEASE_VERSION}.tar.gz.sha256",
            "itambox-${RELEASE_VERSION}.sbom.spdx.json",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, verification)


if __name__ == "__main__":
    unittest.main()
