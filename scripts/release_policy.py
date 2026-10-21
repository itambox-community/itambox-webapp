#!/usr/bin/env python3
"""Validate ITAMbox release identities and release metadata."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path

_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<channel>alpha|beta|rc)\.(?P<number>[1-9]\d*))?$"
)
_CHANNEL_ORDER = {"alpha": 0, "beta": 1, "rc": 2, None: 3}
_PEP440_CHANNEL = {"alpha": "a", "beta": "b", "rc": "rc"}


class ReleasePolicyError(ValueError):
    """Raised when release metadata violates the repository policy."""


@total_ordering
@dataclass(frozen=True)
class ReleaseVersion:
    semver: str
    major: int
    minor: int
    patch: int
    channel: str | None
    number: int | None

    @property
    def pep440(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.channel is None:
            return base
        return f"{base}{_PEP440_CHANNEL[self.channel]}{self.number}"

    @property
    def is_prerelease(self) -> bool:
        return self.channel is not None

    def _sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.patch,
            _CHANNEL_ORDER[self.channel],
            self.number or 0,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseVersion):
            return NotImplemented
        return self._sort_key() < other._sort_key()


def parse_version(value: str) -> ReleaseVersion:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise ReleasePolicyError(
            f"invalid release version {value!r}; expected X.Y.Z or X.Y.Z-alpha.N, X.Y.Z-beta.N, or X.Y.Z-rc.N"
        )
    groups = match.groupdict()
    return ReleaseVersion(
        semver=value,
        major=int(groups["major"]),
        minor=int(groups["minor"]),
        patch=int(groups["patch"]),
        channel=groups["channel"],
        number=int(groups["number"]) if groups["number"] else None,
    )


def _read_source_version(path: Path) -> str:
    match = re.fullmatch(
        r'\s*VERSION\s*=\s*["\'](?P<version>[^"\']+)["\']\s*',
        path.read_text(encoding="utf-8"),
    )
    if match is None:
        raise ReleasePolicyError(f"{path.name} must contain one VERSION assignment")
    return match.group("version")


def _read_locked_project_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    matches = [
        package["version"]
        for package in data.get("package", [])
        if package.get("name") == "itambox" and package.get("source", {}).get("virtual") == "."
    ]
    if len(matches) != 1:
        raise ReleasePolicyError("uv.lock must contain one virtual itambox package")
    return matches[0]


def _read_readme_version(path: Path) -> str:
    match = re.search(
        r"This repository is pre-release\. `(?P<version>[^`]+)` is current version metadata",
        path.read_text(encoding="utf-8"),
    )
    if match is None:
        raise ReleasePolicyError("README.md must declare the current version metadata")
    return match.group("version")


def extract_release_notes(path: Path, version: str) -> str:
    text = path.read_text(encoding="utf-8")
    heading = re.search(
        rf"(?m)^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$",
        text,
    )
    if heading is None:
        raise ReleasePolicyError(f"CHANGELOG.md has no dated [{version}] release section")
    following = text[heading.end() :]
    boundaries = [
        match.start()
        for pattern in (r"(?m)^## ", r"(?m)^\[[^]]+\]:\s+https?://")
        if (match := re.search(pattern, following)) is not None
    ]
    end = heading.end() + min(boundaries) if boundaries else len(text)
    notes = text[heading.end() : end].strip()
    if not notes:
        raise ReleasePolicyError(f"CHANGELOG.md release notes for {version} are empty")
    return notes


def validate_repository(root: Path, expected_version: str | None = None) -> ReleaseVersion:
    project_data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project_raw = project_data["project"]["version"]
    source_raw = _read_source_version(root / "itambox" / "itambox" / "release.py")
    locked_raw = _read_locked_project_version(root / "uv.lock")
    readme_raw = _read_readme_version(root / "README.md")

    project_version = parse_version(project_raw)
    source_version = parse_version(source_raw)
    if project_version.semver != source_version.semver:
        raise ReleasePolicyError(
            f"release.py does not match pyproject.toml: {source_version.semver!r} != {project_version.semver!r}"
        )
    if locked_raw != project_version.pep440:
        raise ReleasePolicyError(
            f"uv.lock version {locked_raw!r} does not match pyproject.toml identity {project_version.pep440!r}"
        )
    if readme_raw != project_version.semver:
        raise ReleasePolicyError(
            f"README.md version {readme_raw!r} does not match pyproject.toml version {project_version.semver!r}"
        )
    if expected_version is not None and project_version.semver != expected_version:
        raise ReleasePolicyError(
            f"pyproject.toml version {project_version.semver!r} does not match requested release {expected_version!r}"
        )

    extract_release_notes(root / "CHANGELOG.md", project_version.semver)
    release_link = f"[{project_version.semver}]: https://github.com/itambox-community/itambox-webapp/releases/tag/v{project_version.semver}"
    if release_link not in (root / "CHANGELOG.md").read_text(encoding="utf-8"):
        raise ReleasePolicyError(f"CHANGELOG.md is missing the release link for {project_version.semver}")
    return project_version


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("verify", "notes"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--root",
            type=Path,
            default=Path(__file__).resolve().parents[1],
            help="repository root",
        )
        subparser.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    version = validate_repository(args.root.resolve(), expected_version=args.version)
    if args.command == "verify":
        print(f"release metadata valid: {version.semver} (PEP 440: {version.pep440}, tag: v{version.semver})")
    else:
        print(extract_release_notes(args.root / "CHANGELOG.md", version.semver))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleasePolicyError as exc:
        print(f"release policy error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
