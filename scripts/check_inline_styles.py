"""Fail-closed gate for first-party inline-style and style-element policy.

Browser-delivered HTML must not contain ``style=`` attributes or un-nonced
``<style>`` elements. Authored TypeScript/JavaScript must not write CSS through
DOM style APIs. Python HTML emitters are scanned for the same attribute pattern.

The three explicit exceptions are self-contained HTML consumed by non-browser
sinks: the PDF-only label sanitizer, the xhtml2pdf label document, and the
chart helper used for standalone report/email/PDF output. The chart helper
emits the request nonce whenever it runs in an HTTP request; the PDF-only
sanitizer and label document never become browser responses. These exceptions
are centralized here so they cannot be hidden in product code.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = frozenset({".html", ".py", ".ts", ".js"})
EXCLUDED_PARTS = frozenset({".git", "__pycache__", "build", "docs", "locale", "migrations", "node_modules", "tests"})
PDF_STYLE_EXCEPTIONS = {
    "itambox/core/html_sanitizer.py": (
        "sanitize_label_html_for_pdf emits a sanitized style block only for the isolated xhtml2pdf sink"
    ),
    "itambox/core/reports/charts.py": (
        "chart CSS is emitted with the request nonce for browser reports and without one "
        "only for standalone email/PDF output"
    ),
    "itambox/extras/services/labels.py": "self-contained HTML is consumed by xhtml2pdf and is not sent as browser HTML",
}
UNSAFE_INLINE_EXCEPTIONS = {
    "itambox/itambox/middleware.py": re.compile(r"style-src-attr 'unsafe-inline'", re.IGNORECASE),
}

STYLE_ATTRIBUTE_RE = re.compile(r"\bstyle\s*=\s*['\"]", re.IGNORECASE)
HTML_STYLE_ATTRIBUTE_RE = re.compile(r"<[A-Za-z][^>]*\bstyle\s*=\s*['\"]", re.IGNORECASE | re.DOTALL)
STYLE_ELEMENT_RE = re.compile(r"<style\b[^>]*>", re.IGNORECASE)
DOM_STYLE_RE = re.compile(
    r"\.style\.|\.style\[|setAttribute\s*\(\s*['\"]style|\bstyle\s*=\s*['\"]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str


def _is_excluded(relative_path: str) -> bool:
    parts = set(Path(relative_path).parts[:-1])
    return bool(parts & EXCLUDED_PARTS) or "dist" in parts


def tracked_source_files(root: Path = REPO_ROOT) -> list[tuple[Path, str]]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "itambox"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    files = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path:
            continue
        relative = Path(raw_path).as_posix()
        if _is_excluded(relative) or Path(relative).suffix not in SOURCE_SUFFIXES:
            continue
        files.append((root / relative, relative))
    return files


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_source(relative_path: str, text: str) -> list[Finding]:
    """Scan one source file and return policy findings in stable order."""
    suffix = Path(relative_path).suffix.lower()
    findings = _unsafe_inline_findings(relative_path, text)
    scanner = {".html": _scan_html, ".py": _scan_python, ".ts": _scan_frontend, ".js": _scan_frontend}.get(suffix)
    if scanner:
        findings.extend(scanner(relative_path, text))
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule))


def _unsafe_inline_findings(relative_path: str, text: str) -> list[Finding]:
    allowed = UNSAFE_INLINE_EXCEPTIONS.get(relative_path)
    if allowed:
        text = allowed.sub("style-src-attr <runtime-library-exception>", text)
    return _findings_for_matches(
        relative_path,
        text,
        re.finditer(r"unsafe-inline", text, re.IGNORECASE),
        "CSP-STYLE1",
        "production source contains unsafe-inline",
    )


def _scan_html(relative_path: str, text: str) -> list[Finding]:
    findings = _findings_for_matches(
        relative_path,
        text,
        STYLE_ATTRIBUTE_RE.finditer(text),
        "CSP-STYLE2",
        "HTML contains a style attribute",
    )
    for match in STYLE_ELEMENT_RE.finditer(text):
        if not re.search(r"\bnonce\s*=", match.group(0), re.IGNORECASE):
            findings.append(
                Finding(
                    relative_path,
                    _line_number(text, match.start()),
                    "CSP-STYLE3",
                    "browser HTML style element has no nonce",
                )
            )
    return findings


def _scan_python(relative_path: str, text: str) -> list[Finding]:
    if relative_path in PDF_STYLE_EXCEPTIONS:
        return []
    findings = _findings_for_matches(
        relative_path,
        text,
        HTML_STYLE_ATTRIBUTE_RE.finditer(text),
        "CSP-STYLE4",
        "Python HTML emitter contains a style attribute",
    )
    for match in STYLE_ELEMENT_RE.finditer(text):
        if not re.search(r"\bnonce\s*=", match.group(0), re.IGNORECASE):
            findings.append(
                Finding(
                    relative_path,
                    _line_number(text, match.start()),
                    "CSP-STYLE5",
                    "Python HTML style element has no nonce",
                )
            )
    return findings


def _scan_frontend(relative_path: str, text: str) -> list[Finding]:
    return _findings_for_matches(
        relative_path,
        text,
        DOM_STYLE_RE.finditer(text),
        "CSP-STYLE6",
        "frontend source writes an inline DOM style",
    )


def _findings_for_matches(relative_path, text, matches, rule, message) -> list[Finding]:
    return [Finding(relative_path, _line_number(text, match.start()), rule, message) for match in matches]


def scan_repository(root: Path = REPO_ROOT) -> list[Finding]:
    files = tracked_source_files(root)
    if not files:
        raise RuntimeError("inline-style policy inventory is empty")
    findings = []
    for path, relative in files:
        findings.extend(scan_source(relative, path.read_text(encoding="utf-8")))
    return sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        findings = scan_repository()
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"inline-style policy could not establish a trustworthy inventory: {exc}", file=sys.stderr)
        return 2
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.message}", file=sys.stderr)
        print(f"inline-style policy failed with {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("inline-style policy passed: tracked production HTML emitters contain no unapproved inline styles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
