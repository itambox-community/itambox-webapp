"""Fail when user-facing product language contains en or em dashes.

The scan is deliberately scope-aware:
* productive Python gettext/ngettext calls only;
* rendered HTML after Django/HTML/CSS comment blocks are removed;
* active gettext message fields in the German django/djangojs catalogs.

Developer docs, comments, tests, migrations, and historical prose are not product
language and are intentionally excluded.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATTERNS = {
    "en dash": re.compile(r"\u2013|&ndash;|&#(?:8211|x0*2013);|\\u(?:2013|\{0*2013\})|\\U0*2013|\\N\{EN DASH\}", re.I),
    "em dash": re.compile(r"\u2014|&mdash;|&#(?:8212|x0*2014);|\\u(?:2014|\{0*2014\})|\\U0*2014|\\N\{EM DASH\}", re.I),
}
TRANSLATION_CALLS = {
    "_": (0,),
    "gettext": (0,),
    "gettext_lazy": (0,),
    "gettext_noop": (0,),
    "ngettext": (0, 1),
    "pgettext": (1,),
    "npgettext": (1, 2),
    "_lazy": (0,),
}
PRESENTATION_CALLS = {
    "format_html": (0,),
    "format_html_join": (1,),
    "mark_safe": (0,),
    "HTML": (0,),
    "Submit": (1,),
}
JS_TRANSLATION_CALL = re.compile(r"\b(?:gettext|ngettext|pgettext|npgettext)\s*\(")
JS_STRING = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.S)
JS_CODEPOINT_CALL = re.compile(r"\bString\.from(?:CharCode|CodePoint)\s*\(\s*(0x[0-9a-f]+|\d+)\s*\)", re.I)
JS_DOM_TEXT_ASSIGNMENT = re.compile(r"\b(?:textContent|innerText|innerHTML)\s*(?:\+=|=)\s*(?P<expression>[^;\n]+)")
JS_DOM_ATTRIBUTE_ASSIGNMENT = re.compile(
    r"\bsetAttribute\s*\(\s*['\"](?:title|aria-label|placeholder)['\"]\s*,\s*"
    r"(?P<expression>[^;\n]+)"
)
EXCLUDED_PARTS = {"tests", "migrations", "docs", "static", "locale"}
TABLE_EMPTY_VALUE_PATH = "itambox/core/tables/constants.py"
TABLE_EMPTY_VALUE_NAME = "TABLE_EMPTY_VALUE"

# These exact current-main identities are persisted migration or API schema
# contracts. Their visible form help text is overridden in the presentation
# layer. Keep this allowlist path- and message-specific so it cannot become a
# general model, API, or punctuation exclusion.
FROZEN_CONTRACT_COPY = {
    (
        "itambox/assets/models/asset.py",
        "Override depreciation policy — leave empty to use the tenant default or asset-type schedule.",
    ),
    (
        "itambox/assets/models/catalog.py",
        "Barcode (EAN / UPC / GTIN) — scanning shows assets of this type.",
    ),
    (
        "itambox/extras/models.py",
        "Unresolved — operator review required",
    ),
    (
        "itambox/inventory/abstract_models.py",
        "Barcode (EAN / UPC / GTIN) — scannable to open this item.",
    ),
    (
        "itambox/inventory/abstract_models.py",
        "Owning tenant — always the stock location's tenant.",
    ),
    (
        "itambox/licenses/models.py",
        "Optional version constraint for this license entitlement (e.g. '2021', '16.x'). "
        "Informational only — reconciliation is performed at the Software level (version-agnostic).",
    ),
    (
        "itambox/itambox/api/pagination.py",
        "Keyset/cursor pagination: return results with pk >= start, ordered by pk. "
        "Skips the (capped) row count and stays O(page) regardless of table size — "
        "use this instead of offset/limit for bulk export or iterating large collections. "
        "Follow the `next` link to walk subsequent pages.",
    ),
}

# These exact literal fragments are retained only in model string
# representations for compatibility with stored/audit/API-facing values. The
# rest of each expression remains subject to the product-language scan.
FROZEN_STRING_LITERALS = {
    ("itambox/assets/models/lifecycle.py", "Warranty", " – "),
    ("itambox/assets/models/lifecycle.py", "AssetReservation", " – "),
    ("itambox/organization/models.py", "CostCenter", " – "),
    ("itambox/procurement/models.py", "Contract", " – "),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    field: str
    tokens: tuple[str, ...]


def forbidden_tokens(value: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in FORBIDDEN_PATTERNS.items() if pattern.search(value))


def integer_constant(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = integer_constant(node.operand)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
    return None


def constant_text(node: ast.AST) -> str | None:
    """Evaluate deliberately simple string construction used in product copy."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = constant_text(node.left)
        right = constant_text(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return joined_text(node)
    if isinstance(node, ast.Call):
        return call_text(node)
    return None


def joined_text(node: ast.JoinedStr) -> str | None:
    parts = []
    for value in node.values:
        argument = value.value if isinstance(value, ast.FormattedValue) else value
        part = constant_text(argument)
        if part is None:
            number = integer_constant(argument)
            part = str(number) if number is not None else None
        if part is None:
            return None
        parts.append(part)
    return "".join(parts)


def call_text(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name) and node.func.id == "chr" and len(node.args) == 1:
        value = integer_constant(node.args[0])
        if value is None:
            return None
        try:
            return chr(value)
        except ValueError:
            return None
    if not isinstance(node.func, ast.Attribute):
        return None
    base = constant_text(node.func.value)
    if node.func.attr == "join":
        return joined_call_text(base, node.args)
    if node.func.attr == "format":
        return formatted_call_text(base, node.args)
    return None


def joined_call_text(base: str | None, arguments: list[ast.expr]) -> str | None:
    if base is None or len(arguments) != 1 or not isinstance(arguments[0], (ast.List, ast.Tuple)):
        return None
    values = [constant_text(item) for item in arguments[0].elts]
    if any(value is None for value in values):
        return None
    return base.join(value for value in values if value is not None)


def formatted_call_text(base: str | None, arguments: list[ast.expr]) -> str | None:
    if base is None:
        return None
    values: list[str | int] = []
    for argument in arguments:
        text_value = constant_text(argument)
        number_value = integer_constant(argument)
        if text_value is not None:
            values.append(text_value)
        elif number_value is not None:
            values.append(number_value)
        else:
            return None
    try:
        return base.format(*values)
    except (IndexError, KeyError, ValueError):
        return None


def tracked_files() -> list[str]:
    output = subprocess.check_output(["git", "ls-files"], cwd=REPOSITORY_ROOT, text=True)
    return output.splitlines()


def scan_table_empty_value(tree: ast.AST, relative: str) -> list[Finding]:
    if relative != TABLE_EMPTY_VALUE_PATH or not isinstance(tree, ast.Module):
        return []

    findings = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        else:
            continue

        if not any(isinstance(target, ast.Name) and target.id == TABLE_EMPTY_VALUE_NAME for target in targets):
            continue

        value = constant_text(value_node) if value_node is not None else None
        tokens = forbidden_tokens(value) if value is not None else ()
        if tokens:
            findings.append(Finding(relative, node.lineno, TABLE_EMPTY_VALUE_NAME, tokens))
    return findings


def scan_python(path: Path, relative: str) -> list[Finding]:
    if set(Path(relative).parts) & EXCLUDED_PARTS or relative.startswith("scripts/"):
        return []
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
    findings = scan_python_calls(tree, relative)
    findings.extend(scan_python_direct_ui_literals(tree, relative))
    findings.extend(scan_table_empty_value(tree, relative))
    if relative == "itambox/itambox/api/pagination.py":
        findings.extend(scan_api_descriptions(tree, relative))
    unique = []
    seen = set()
    for finding in findings:
        identity = (finding.path, finding.line, finding.field, finding.tokens)
        if identity not in seen:
            seen.add(identity)
            unique.append(finding)
    return unique


DIRECT_UI_KEYWORDS = {"label", "help_text", "message", "placeholder", "title", "verbose_name"}
DIRECT_UI_DICT_KEYS = {"placeholder", "title", "aria-label", "aria_label"}
DIRECT_UI_CALLS = {"ValidationError", "PermissionDenied", "SuspiciousOperation", "add_error", "message_user"}


def joined_fragments(node: ast.JoinedStr) -> list[str]:
    fragments = []
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            fragment = constant_text(value.value)
            if fragment is not None:
                fragments.append(fragment)
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            fragments.append(value.value)
    return fragments


def literal_fragments(node: ast.AST) -> list[str]:
    """Return literal portions of a simple expression, including f-strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return joined_fragments(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return literal_fragments(node.left) + literal_fragments(node.right)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [fragment for item in node.elts for fragment in literal_fragments(item)]
    if isinstance(node, ast.Dict):
        return [fragment for item in node.values if item is not None for fragment in literal_fragments(item)]
    value = constant_text(node)
    return [value] if value is not None else []


class PythonProductCopyVisitor(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.findings: list[Finding] = []

    def visit_ClassDef(self, node: ast.ClassDef):
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Return(self, node: ast.Return):
        if self.function_stack and self.function_stack[-1] == "__str__":
            self._scan_expression(node.value, "__str__", node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        if any(isinstance(target, ast.Name) and target.id in {"help_texts", "labels"} for target in node.targets):
            self._scan_expression(node.value, "form copy", node.lineno, include_fragments=True)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        function_name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        if function_name in DIRECT_UI_CALLS:
            positions = (0,)
            if function_name in {"add_error", "message_user"}:
                positions = (1,)
            for position in positions:
                if position < len(node.args):
                    self._scan_expression(node.args[position], function_name, node.lineno)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            if node.func.value.id == "messages" and len(node.args) > 1:
                self._scan_expression(node.args[1], f"messages.{node.func.attr}", node.lineno)
        for keyword in node.keywords:
            if keyword.arg in DIRECT_UI_KEYWORDS:
                self._scan_expression(keyword.value, keyword.arg, node.lineno)
            elif keyword.arg == "choices":
                self._scan_expression(keyword.value, "choices", node.lineno, include_fragments=True)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value in DIRECT_UI_DICT_KEYS:
                self._scan_expression(value, str(key.value), node.lineno)
        self.generic_visit(node)

    def _scan_expression(self, node: ast.AST | None, field: str, line: int, *, include_fragments: bool = False):
        if node is None:
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in TRANSLATION_CALLS or node.func.id in PRESENTATION_CALLS:
                return
        values = literal_fragments(node) if include_fragments or constant_text(node) is None else [constant_text(node)]
        class_name = self.class_stack[-1] if self.class_stack else None
        for value in values:
            if value is None or (self.relative, value) in FROZEN_CONTRACT_COPY:
                continue
            if (self.relative, class_name, value) in FROZEN_STRING_LITERALS:
                continue
            tokens = forbidden_tokens(value)
            if tokens:
                self.findings.append(Finding(self.relative, line, field, tokens))


def scan_python_direct_ui_literals(tree: ast.AST, relative: str) -> list[Finding]:
    visitor = PythonProductCopyVisitor(relative)
    visitor.visit(tree)
    return visitor.findings


def scan_python_calls(tree: ast.AST, relative: str) -> list[Finding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        positions = TRANSLATION_CALLS.get(node.func.id) or PRESENTATION_CALLS.get(node.func.id)
        if positions is None:
            continue
        for position in positions:
            if position >= len(node.args):
                continue
            argument = node.args[position]
            value = constant_text(argument)
            values = [value] if value is not None else literal_fragments(argument)
            for value in values:
                if value is None or (relative, value) in FROZEN_CONTRACT_COPY:
                    continue
                tokens = forbidden_tokens(value)
                if tokens:
                    findings.append(Finding(relative, node.lineno, node.func.id, tokens))
    return findings


def scan_api_descriptions(tree: ast.AST, relative: str) -> list[Finding]:
    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, argument in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant) or key.value != "description":
                continue
            value = constant_text(argument)
            if value is None or (relative, value) in FROZEN_CONTRACT_COPY:
                continue
            tokens = forbidden_tokens(value)
            if tokens:
                findings.append(Finding(relative, node.lineno, "API description", tokens))
    return findings


def strip_template_comments(source: str) -> str:
    for pattern in (
        r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}",
        r"{#.*?#}",
        r"<!--.*?-->",
        r"/\*.*?\*/",
    ):
        source = re.sub(pattern, lambda match: "\n" * match.group(0).count("\n"), source, flags=re.S)
    return source


def scan_template(path: Path, relative: str) -> list[Finding]:
    source = strip_template_comments(path.read_text(encoding="utf-8-sig"))
    findings = []
    for line_number, line in enumerate(source.splitlines(), 1):
        tokens = forbidden_tokens(line)
        if tokens:
            findings.append(Finding(relative, line_number, "rendered template", tokens))
    return findings


def scan_javascript_translation_body(body: str, relative: str, line_number: int) -> list[Finding]:
    findings = []
    values = []
    for literal in JS_STRING.findall(body):
        value = decode_js_literal(literal) or literal
        values.append(value)
        tokens = forbidden_tokens(value)
        if tokens:
            findings.append(Finding(relative, line_number, "translation call", tokens))
    tokens = forbidden_tokens("".join(values))
    if tokens and not any(finding.tokens == tokens for finding in findings):
        findings.append(Finding(relative, line_number, "constructed translation call", tokens))
    for codepoint in JS_CODEPOINT_CALL.findall(body):
        tokens = forbidden_tokens(chr(int(codepoint, 0)))
        if tokens:
            findings.append(Finding(relative, line_number, "constructed translation call", tokens))
    return findings


def scan_javascript_translation_calls(source: str, relative: str) -> list[Finding]:
    findings = []
    for call in JS_TRANSLATION_CALL.finditer(source):
        end = find_js_call_end(source, call.end())
        if end is not None:
            line_number = source.count("\n", 0, call.start()) + 1
            findings.extend(scan_javascript_translation_body(source[call.end() : end - 1], relative, line_number))
    return findings


def javascript_expression_values(expression: str) -> list[str]:
    values = []
    for literal in JS_STRING.findall(expression):
        value = decode_js_literal(literal)
        if value is not None:
            values.append(value)
    values.extend(chr(int(codepoint, 0)) for codepoint in JS_CODEPOINT_CALL.findall(expression))
    return values


def scan_javascript_dom_assignments(source: str, relative: str) -> list[Finding]:
    findings = []
    for pattern, field in (
        (JS_DOM_TEXT_ASSIGNMENT, "DOM text"),
        (JS_DOM_ATTRIBUTE_ASSIGNMENT, "DOM attribute"),
    ):
        for match in pattern.finditer(source):
            values = javascript_expression_values(match.group("expression"))
            tokens = forbidden_tokens("".join(values))
            if tokens:
                line_number = source.count("\n", 0, match.start()) + 1
                findings.append(Finding(relative, line_number, field, tokens))
    return findings


def scan_javascript(path: Path, relative: str) -> list[Finding]:
    source = path.read_text(encoding="utf-8-sig")
    return scan_javascript_translation_calls(source, relative) + scan_javascript_dom_assignments(source, relative)


def update_js_quote(char: str, quote: str | None, escaped: bool) -> tuple[str | None, bool]:
    if escaped:
        return quote, False
    if char == "\\":
        return quote, True
    if char == quote:
        return None, False
    return quote, False


def find_js_call_end(text: str, start: int) -> int | None:
    depth = 1
    quote = None
    escaped = False
    index = start
    while index < len(text) and depth:
        char = text[index]
        if quote:
            quote, escaped = update_js_quote(char, quote, escaped)
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return index if depth == 0 else None


def decode_js_literal(literal: str) -> str | None:
    if literal.startswith("`"):
        literal = repr(literal[1:-1])
    try:
        return ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None


def scan_po(path: Path, relative: str) -> list[Finding]:
    findings = []
    source = path.read_text(encoding="utf-8-sig")
    line_offset = 0
    for block in source.split("\n\n"):
        lines = block.splitlines()
        if not block.strip() or block.lstrip().startswith("#~"):
            line_offset += len(lines) + 1
            continue
        fields, references = po_fields(lines, line_offset)
        findings.extend(scan_po_fields(fields, relative, references))
        line_offset += len(lines) + 1
    return findings


def literal_value(literal: str) -> str:
    try:
        return ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return literal


def po_fields(lines: list[str], line_offset: int) -> tuple[dict[str, tuple[int, str]], tuple[str, ...]]:
    fields: dict[str, tuple[int, str]] = {}
    references = []
    active_field = None
    for index, raw in enumerate(lines, 1):
        line = raw.strip()
        if line.startswith("#:"):
            references.extend(normalize_po_reference(token) for token in line[2:].split())
            continue
        if line.startswith("#"):
            continue
        match = re.match(r"(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(.*)$", line)
        if match:
            active_field, literal = match.groups()
            fields[active_field] = (line_offset + index, literal_value(literal))
        elif line.startswith('"') and active_field is not None:
            field_line, current = fields[active_field]
            fields[active_field] = (field_line, current + literal_value(line))
    return fields, tuple(references)


def normalize_po_reference(reference: str) -> str:
    """Return the repository-relative path portion of a PO source reference."""
    reference = reference.replace("\\", "/")
    if ":" in reference:
        reference = reference.rsplit(":", 1)[0]
    return reference.removeprefix("./")


def scan_po_fields(fields: dict[str, tuple[int, str]], relative: str, references: tuple[str, ...]) -> list[Finding]:
    findings = []
    frozen_paths_by_message: dict[str, set[str]] = {}
    for frozen_path, message in FROZEN_CONTRACT_COPY:
        frozen_paths_by_message.setdefault(message, set()).add(frozen_path)
    for field, (line_number, value) in fields.items():
        if field in {"msgid", "msgid_plural"} and value in frozen_paths_by_message:
            allowed_paths = frozen_paths_by_message[value]
            if references and set(references).issubset(allowed_paths):
                continue
        tokens = forbidden_tokens(value)
        if tokens:
            findings.append(Finding(relative, line_number, field, tokens))
    return findings


def scan_repository() -> tuple[list[Finding], dict[str, int]]:
    findings = []
    counts = {"python": 0, "templates": 0, "javascript": 0, "catalogs": 0}
    for relative in tracked_files():
        path = REPOSITORY_ROOT / relative
        if not path.is_file():
            continue
        if relative.endswith(".py"):
            counts["python"] += 1
            findings.extend(scan_python(path, relative))
        elif relative.endswith(".html"):
            counts["templates"] += 1
            findings.extend(scan_template(path, relative))
        elif relative.startswith("itambox/static/src/") and Path(relative).suffix in {".js", ".jsx", ".ts", ".tsx"}:
            counts["javascript"] += 1
            findings.extend(scan_javascript(path, relative))
        elif relative in {
            "itambox/locale/de/LC_MESSAGES/django.po",
            "itambox/locale/de/LC_MESSAGES/djangojs.po",
        }:
            counts["catalogs"] += 1
            findings.extend(scan_po(path, relative))
    return findings, counts


def main() -> int:
    findings, counts = scan_repository()
    if findings:
        print("User-facing product language contains forbidden en/em dash forms:")
        for finding in findings:
            print(f"  {finding.path}:{finding.line} [{finding.field}] {', '.join(finding.tokens)}")
        return 1
    print(
        "Product-language dash gate passed: "
        f"{counts['python']} Python files, {counts['templates']} templates, "
        f"{counts['javascript']} JavaScript/TypeScript files, {counts['catalogs']} catalogs scanned."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
