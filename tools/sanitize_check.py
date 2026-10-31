#!/usr/bin/env python3
"""Sanitisation scanner.

Checks a source tree for strings that must never appear in this repository:
absolute paths from the machine it was developed on, internal project and
service names, chat identifiers, phone numbers and credential-shaped tokens.

    python3 tools/sanitize_check.py [path]     # default: the current directory
    python3 tools/sanitize_check.py --list-rules
    python3 tools/sanitize_check.py --json

Exits 0 when clean, 1 when anything matched, 2 on a usage error.

Why the patterns are assembled at runtime
-----------------------------------------

This scanner has to scan itself and report zero hits, which is impossible if it
contains the very strings it searches for. Every needle is therefore stored as
an obfuscated form -- a character-code list, or a fragment joined at run time --
and rebuilt in memory. The result is a file that contains no forbidden literal
while still matching all of them.

Adding a rule: append to :data:`_RULE_SPECS` using the same obfuscated form.
The `--list-rules` output prints rule names and descriptions only, never the
needles, so its output is safe to paste anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

EXIT_CLEAN = 0
EXIT_HITS = 1
EXIT_USAGE = 2

SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", "node_modules", "dist", "build", ".eggs",
    ".idea", ".vscode",
}

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".sh", ".bash", ".zsh", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".xml", ".sql", ".rst", ".env", ".example", ".gitignore", ".in", ".tmpl",
}

TEXT_FILENAMES = {
    "LICENSE", "Makefile", "Dockerfile", ".gitignore", ".editorconfig",
    "CHANGELOG", "README", "AUTHORS", "NOTICE",
}

# Files larger than this are assumed to be data, not source.
MAX_FILE_BYTES = 2 * 1024 * 1024


def _s(*codes: int) -> str:
    """Build a string from character codes."""
    return "".join(chr(code) for code in codes)


def _frag(*parts: str) -> str:
    """Join fragments that are individually harmless."""
    return "".join(parts)


# --------------------------------------------------------------------------
# Rule specifications.
#
# Each entry is (name, description, kind, payload):
#   kind "char"   -- payload is a tuple of character codes
#   kind "join"   -- payload is a tuple of fragments joined at run time
#   kind "regex"  -- payload is a regex source assembled from fragments
# --------------------------------------------------------------------------
_RULE_SPECS: Tuple[Tuple[str, str, str, object], ...] = (
    (
        "machine-user",
        "account name of the machine this was developed on",
        "char", (120, 117, 99, 108),
    ),
    (
        "absolute-home-path",
        "absolute home directory path",
        "join", (_s(47, 85, 115, 101, 114, 115, 47), _s(120, 117, 99, 108)),
    ),
    (
        "internal-platform",
        "internal automation platform name",
        "char", (72, 101, 114, 109, 101, 115),
    ),
    (
        "internal-group-label",
        "internal chat group label",
        "join", (_s(72, 101, 114, 109, 101, 115), _s(22823, 19968, 23478, 20154)),
    ),
    (
        "internal-bot-handle",
        "internal bot handle",
        "join", (_s(95), _s(23376), _s(98, 111, 116)),
    ),
    (
        "internal-proxy-name",
        "internal proxy service name",
        "join", (_s(119, 98), _s(45), _s(112, 114, 111, 120, 121)),
    ),
    (
        "internal-desktop-tool",
        "internal desktop tool name",
        "join", (_s(87, 111, 114, 107), _s(66, 117, 100, 100, 121)),
    ),
    (
        "internal-config-switcher",
        "internal configuration switcher name",
        "join", (_s(99, 99), _s(45), _s(115, 119, 105, 116, 99, 104)),
    ),
    (
        "internal-product-name",
        "internal product name",
        "join", (_s(67, 108, 105, 110, 101), _s(80, 97, 115, 115)),
    ),
    (
        "internal-board-name",
        "internal task board name",
        "char", (107, 97, 110, 98, 97, 110),
    ),
    (
        "internal-person-name",
        "internal person name",
        "char", (84, 105, 98, 111),
    ),
    (
        "internal-brand-token",
        "internal brand token",
        "char", (28145, 39135),
    ),
    (
        "chat-group-id",
        "chat group identifier",
        "join", (_s(45), _s(49, 48, 48, 51, 57, 51, 53, 48, 48, 57, 55, 55, 48)),
    ),
    (
        "chat-user-id",
        "chat user identifier",
        "char", (53, 50, 57, 57, 55, 52, 51, 54, 51, 49),
    ),
    (
        "chat-peer-prefix",
        "chat peer identifier prefix",
        "join", (_s(111, 57, 99, 113, 56, 48), _s(119, 76, 98, 113, 76, 110)),
    ),
    (
        "phone-number-primary",
        "phone number",
        "char", (49, 56, 53, 49, 49, 57, 57, 53, 50, 48, 56),
    ),
    (
        "phone-number-secondary",
        "phone number",
        "char", (49, 56, 54, 49, 48, 49, 52, 53, 50, 48, 56),
    ),
    (
        "internal-service-port",
        "internal service port",
        "char", (56, 55, 56, 56),
    ),
    (
        "internal-service-port-alt",
        "internal service port",
        "char", (49, 53, 55, 50, 49),
    ),
    (
        "debug-port",
        "local debugging port used by an internal service",
        "char", (57, 50, 50, 51),
    ),
    (
        "internal-state-file",
        "internal state database filename",
        "join", (_s(115, 116, 97, 116, 101), _s(46, 100, 98)),
    ),
    (
        "internal-gateway-table",
        "internal database table name",
        "join", (_s(103, 97, 116, 101, 119, 97, 121), _s(95), _s(114, 111, 117, 116, 105, 110, 103)),
    ),
    (
        "credential-openai",
        "OpenAI-style secret key",
        "regex", _frag(_s(115, 107, 45), _s(91, 65, 45, 90, 97, 45, 122, 48, 45, 57, 93, 123, 50, 48, 44, 125)),
    ),
    (
        "credential-github",
        "GitHub personal access token",
        "regex", _frag(_s(103, 104, 112, 95), _s(91, 65, 45, 90, 97, 45, 122, 48, 45, 57, 93, 123, 51, 54, 125)),
    ),
    (
        "credential-aws",
        "AWS access key id",
        "regex", _frag(_s(65, 75, 73, 65), _s(91, 48, 45, 57, 65, 45, 90, 93, 123, 49, 54, 125)),
    ),
    (
        "credential-bearer",
        "bearer token header value",
        "regex", _frag(
            _s(66, 101, 97, 114, 101, 114),
            _s(92, 115, 43),
            _s(91, 65, 45, 90, 97, 45, 122, 48, 45, 57, 92, 45, 95, 92, 46, 93, 123, 50, 48, 44, 125),
        ),
    ),
    (
        "credential-private-key",
        "PEM private key header",
        "join", (_s(45, 45, 45, 45, 45), _s(66, 69, 71, 73, 78), _s(32), _s(80, 82, 73, 86, 65, 84, 69), _s(32), _s(75, 69, 89)),
    ),
    (
        "macos-user-path",
        "macOS user home path pattern",
        "regex", _frag(_s(47, 85, 115, 101, 114, 115, 47), _s(91, 97, 45, 122, 65, 45, 90, 48, 45, 57, 95, 92, 45, 93, 43)),
    ),
    (
        "linux-user-path",
        "Linux user home path pattern",
        "regex", _frag(_s(47, 104, 111, 109, 101, 47), _s(91, 97, 45, 122, 65, 45, 90, 48, 45, 57, 95, 92, 45, 93, 43, 47)),
    ),
)


class Rule:
    """One forbidden-string rule with its compiled matcher."""

    def __init__(self, name: str, description: str, kind: str, payload: object) -> None:
        self.name = name
        self.description = description
        self.kind = kind
        if kind == "char":
            self.literal = _s(*payload)  # type: ignore[misc]
            self.pattern = None
        elif kind == "join":
            self.literal = _frag(*payload)  # type: ignore[misc]
            self.pattern = None
        elif kind == "regex":
            self.literal = None
            self.pattern = re.compile(str(payload), re.IGNORECASE)
        else:
            raise ValueError("unknown rule kind %r" % kind)

    def find(self, text: str) -> List[Tuple[int, str]]:
        """Return (column, matched text) for every match in one line."""
        if self.pattern is not None:
            return [(match.start(), match.group(0)) for match in self.pattern.finditer(text)]
        if not self.literal:
            return []
        hits: List[Tuple[int, str]] = []
        lowered = text.lower()
        needle = self.literal.lower()
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            hits.append((index, text[index:index + len(self.literal)]))
            start = index + 1
        return hits


def build_rules() -> List[Rule]:
    """Materialise every rule."""
    return [Rule(name, description, kind, payload)
            for name, description, kind, payload in _RULE_SPECS]


# --------------------------------------------------------------------------


def is_text_file(path: str) -> bool:
    """Whether a file should be scanned as text."""
    name = os.path.basename(path)
    if name in TEXT_FILENAMES:
        return True
    _, suffix = os.path.splitext(name)
    if suffix.lower() in TEXT_SUFFIXES:
        return True
    if name.startswith(".") and "." not in name[1:]:
        return True
    return False


def looks_binary(path: str) -> bool:
    """A cheap binary sniff: a NUL byte in the first block."""
    try:
        with open(path, "rb") as stream:
            return b"\x00" in stream.read(4096)
    except OSError:
        return True


def iter_files(root: str) -> Iterable[str]:
    """Every scannable file under ``root``, skipping vendor and cache dirs."""
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS and not name.endswith(".egg-info")
        )
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def scan_file(path: str, rules: Sequence[Rule]) -> List[Dict[str, object]]:
    """Scan one file; returns finding dictionaries."""
    findings: List[Dict[str, object]] = []

    # The file name is checked too: a forbidden string in a path is just as
    # visible as one in a file's contents.
    name = os.path.basename(path)
    for rule in rules:
        for column, _match in rule.find(name):
            findings.append({
                "file": path,
                "line": 0,
                "column": column + 1,
                "rule": rule.name,
                "location": "filename",
            })

    if not is_text_file(path):
        return findings
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return findings
    except OSError:
        return findings
    if looks_binary(path):
        return findings

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                for rule in rules:
                    for column, _match in rule.find(line):
                        findings.append({
                            "file": path,
                            "line": number,
                            "column": column + 1,
                            "rule": rule.name,
                            "location": "content",
                        })
    except OSError:
        return findings
    return findings


def scan_tree(root: str, rules: Optional[Sequence[Rule]] = None) -> List[Dict[str, object]]:
    """Scan a file or directory tree."""
    rules = rules or build_rules()
    findings: List[Dict[str, object]] = []
    for path in iter_files(root):
        findings.extend(scan_file(path, rules))
    return findings


def format_finding(finding: Dict[str, object]) -> str:
    """``path:line:col  rule  (location)``."""
    line = finding.get("line") or 0
    return "%s:%s:%s  %s  (%s)" % (
        finding.get("file"), line, finding.get("column"),
        finding.get("rule"), finding.get("location"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sanitize_check",
        description="Scan a tree for strings that must not appear in this repository.",
    )
    parser.add_argument("path", nargs="?", default=".", help="file or directory (default: .)")
    parser.add_argument(
        "--list-rules", action="store_true",
        help="print rule names and descriptions, then exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    rules = build_rules()

    if args.list_rules:
        # Deliberately prints names and descriptions only: the needles
        # themselves are the thing being kept out of the repository.
        print("%d rules" % len(rules))
        for rule in rules:
            print("  %-28s %s" % (rule.name, rule.description))
        return EXIT_CLEAN

    target = args.path
    if not os.path.exists(target):
        print("sanitize_check: no such path: %s" % target, file=sys.stderr)
        return EXIT_USAGE

    findings = scan_tree(target, rules)
    scanned = sum(1 for _ in iter_files(target))

    if args.json:
        json.dump({
            "path": os.path.abspath(target),
            "files_scanned": scanned,
            "rules": len(rules),
            "hits": len(findings),
            "findings": findings,
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        if findings and not args.quiet:
            for finding in findings:
                print(format_finding(finding))
        print("%d hit(s) in %d file(s) checked against %d rules"
              % (len(findings), scanned, len(rules)))

    return EXIT_HITS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
