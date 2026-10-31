"""The sanitisation scanner.

Two properties matter, and they pull against each other. The scanner must catch
every forbidden string, and it must itself be clean enough to live in the
repository it polices -- which means the needles cannot be written down here
either. They are synthesized from the rule table at run time instead, so this
file contains no forbidden literal while still planting all of them.

The last test scans the real repository. It is the one that would have caught a
transcript or an absolute path slipping into a fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_PATH = os.path.join(REPO_ROOT, "tools", "sanitize_check.py")


def load_scanner():
    """Import ``tools/sanitize_check.py`` by path.

    It is a script, not part of the installed package, so it is loaded rather
    than imported; adding ``tools/`` to ``sys.path`` would put a second copy of
    a top-level name into every other test module's import space.
    """
    spec = importlib.util.spec_from_file_location("sanitize_check_under_test", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scanner = load_scanner()


# --------------------------------------------------------------- needles


def sample_from_pattern(source: str) -> str:
    """Build a string that one of this scanner's own patterns must match.

    Every regex rule here has the same shape -- a literal prefix, one character
    class with a repetition count, and sometimes a literal tail -- which is
    enough structure to synthesize an example without pulling in a regex
    generator. Asserting the sample really matches is left to the caller.
    """
    head, bracket, rest = source.partition("[")
    prefix = head.replace(r"\s+", " ").replace(r"\s*", " ").replace("\\", "")
    if not bracket:
        return prefix

    klass, _, tail = rest.partition("]")
    repetition = re.search(r"\{(\d+)(?:,\d*)?\}", tail)
    if repetition:
        count = int(repetition.group(1))
        tail_literal = tail[repetition.end():]
    else:
        count = 12
        tail_literal = re.sub(r"[+*?]", "", tail)

    filler = ""
    for candidate in ("A", "a", "0", "_", "-", "."):
        if re.fullmatch("[%s]" % klass, candidate):
            filler = candidate
            break
    assert filler, "no usable character for the class [%s]" % klass
    return prefix + filler * count + tail_literal


def needle_for(name: str) -> str:
    """The literal that rule ``name`` exists to find."""
    for rule_name, description, kind, payload in scanner._RULE_SPECS:
        if rule_name != name:
            continue
        if kind == "regex":
            return sample_from_pattern(str(payload))
        return scanner.Rule(rule_name, description, kind, payload).literal
    raise KeyError(name)


RULE_NAMES = [spec[0] for spec in scanner._RULE_SPECS]


@pytest.mark.parametrize("name", RULE_NAMES)
def test_every_rule_finds_its_own_needle(name):
    """The builder above and the rule table have to agree, or a test that
    plants nothing would pass while looking like it planted something."""
    needle = needle_for(name)
    assert needle, name
    rule = [item for item in scanner.build_rules() if item.name == name][0]
    assert rule.find("prefix %s suffix" % needle), name


def test_the_rules_are_not_all_the_same_shape():
    """A guard on the guard: if every needle were identical, the parametrised
    test above would be one test wearing many names."""
    needles = {needle_for(name) for name in RULE_NAMES}
    assert len(needles) == len(RULE_NAMES)


# ------------------------------------------------------------- temp trees


def write_tree(root, files):
    for relative, text in files.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)
    return root


def run_tool(*args, cwd=None):
    return subprocess.run(
        [sys.executable, TOOL_PATH] + list(args),
        cwd=cwd or REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True,
    )


@pytest.mark.parametrize("name", RULE_NAMES)
def test_a_planted_needle_is_reported(tmp_path, name):
    """Each rule finds its own needle, at the right place.

    The rules are independent matchers and some needles contain others --
    the internal group label embeds the platform name, and the absolute home
    path embeds the account name -- so a plant can legitimately produce more
    than one finding. What is asserted is that this rule is among them, at the
    column where the needle actually starts.
    """
    root = write_tree(str(tmp_path / "tree"), {
        "notes.md": "a clean line\nand one with %s in it\n" % needle_for(name),
    })
    findings = scanner.scan_tree(root)
    matching = [finding for finding in findings if finding["rule"] == name]
    assert matching, [finding["rule"] for finding in findings]
    assert matching[0]["line"] == 2
    assert matching[0]["location"] == "content"
    assert matching[0]["column"] == len("and one with ") + 1
    # Nothing was reported on the clean line.
    assert {finding["line"] for finding in findings} == {2}


def test_a_planted_needle_makes_the_command_fail(tmp_path):
    root = write_tree(str(tmp_path / "tree"), {"notes.md": needle_for("internal-platform")})
    result = run_tool(root, "--json")
    assert result.returncode == scanner.EXIT_HITS
    payload = json.loads(result.stdout)
    assert payload["hits"] == 1
    assert payload["findings"][0]["rule"] == "internal-platform"
    assert payload["files_scanned"] == 1
    assert payload["rules"] == len(scanner.build_rules())


def test_a_clean_tree_passes(tmp_path):
    root = write_tree(str(tmp_path / "tree"), {
        "README.md": "# A synthetic project\n\nNothing forbidden here.\n",
        "src/main.py": "def main():\n    return 0\n",
        "config.yaml": "key: value\n",
    })
    assert scanner.scan_tree(root) == []
    result = run_tool(root)
    assert result.returncode == scanner.EXIT_CLEAN
    assert "0 hit(s) in 3 file(s) checked" in result.stdout


def test_a_needle_in_a_filename_is_reported(tmp_path):
    """A forbidden string in a path is as visible as one in a file's body."""
    needle = needle_for("internal-platform")
    root = write_tree(str(tmp_path / "tree"), {"%s-notes.md" % needle: "clean body\n"})
    findings = scanner.scan_tree(root)
    assert [finding["location"] for finding in findings] == ["filename"]
    assert findings[0]["line"] == 0


def test_the_match_is_case_insensitive(tmp_path):
    needle = needle_for("internal-platform")
    root = write_tree(str(tmp_path / "tree"), {"notes.md": needle.upper() + "\n"})
    assert scanner.scan_tree(root)


def test_cached_and_vendor_directories_are_skipped(tmp_path):
    needle = needle_for("internal-platform")
    root = write_tree(str(tmp_path / "tree"), {
        "keep.txt": "clean\n",
        ".git/objects/notes.txt": needle + "\n",
        "__pycache__/mod.py": needle + "\n",
        "node_modules/pkg/index.js": needle + "\n",
        ".venv/lib/mod.py": needle + "\n",
    })
    assert scanner.scan_tree(root) == []


def test_binary_files_are_skipped(tmp_path):
    needle = needle_for("internal-platform")
    path = os.path.join(str(tmp_path / "tree"), "blob.py")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(b"\x00\x01" + needle.encode("utf-8"))
    assert scanner.scan_tree(str(tmp_path / "tree")) == []


def test_only_text_suffixes_are_scanned(tmp_path):
    needle = needle_for("internal-platform")
    root = write_tree(str(tmp_path / "tree"), {
        "data.bin": needle + "\n",
        "image.png": needle + "\n",
        "notes.md": "clean\n",
    })
    assert scanner.scan_tree(root) == []
    # The same content under a source suffix is caught, so the skip is about the
    # file type and not about the string going unnoticed.
    os.remove(os.path.join(root, "data.bin"))
    root = write_tree(root, {"data.py": needle + "\n"})
    assert scanner.scan_tree(root)


def test_an_oversized_file_is_skipped(tmp_path, monkeypatch):
    needle = needle_for("internal-platform")
    root = write_tree(str(tmp_path / "tree"), {"big.md": needle + "\n"})
    monkeypatch.setattr(scanner, "MAX_FILE_BYTES", 4)
    assert scanner.scan_tree(root) == []


def test_several_needles_on_one_line_are_all_reported(tmp_path):
    root = write_tree(str(tmp_path / "tree"), {
        "notes.md": "%s and %s\n" % (needle_for("internal-platform"),
                                     needle_for("internal-board-name")),
    })
    rules = sorted(finding["rule"] for finding in scanner.scan_tree(root))
    assert rules == ["internal-board-name", "internal-platform"]


def test_a_missing_path_is_a_usage_error(tmp_path):
    result = run_tool(str(tmp_path / "absent"))
    assert result.returncode == scanner.EXIT_USAGE
    assert "no such path" in result.stderr


def test_a_single_file_can_be_scanned(tmp_path):
    path = os.path.join(str(tmp_path), "notes.md")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(needle_for("internal-platform") + "\n")
    assert len(scanner.scan_tree(path)) == 1
    assert run_tool(path).returncode == scanner.EXIT_HITS


def test_quiet_prints_only_the_summary(tmp_path):
    root = write_tree(str(tmp_path / "tree"), {"notes.md": needle_for("internal-platform")})
    result = run_tool(root, "--quiet")
    assert result.returncode == scanner.EXIT_HITS
    assert result.stdout.strip().endswith("1 hit(s) in 1 file(s) checked against %d rules"
                                          % len(scanner.build_rules()))
    assert needle_for("internal-platform") not in result.stdout


def test_a_report_names_the_rule_but_not_the_needle(tmp_path):
    """Output gets pasted into issues and reports, so it must not carry the
    string it found."""
    root = write_tree(str(tmp_path / "tree"), {"notes.md": needle_for("chat-group-id")})
    result = run_tool(root)
    assert "chat-group-id" in result.stdout
    assert needle_for("chat-group-id") not in result.stdout


# ------------------------------------------------------------- rule listing


def test_list_rules_prints_names_and_descriptions_only():
    result = run_tool("--list-rules")
    assert result.returncode == scanner.EXIT_CLEAN
    assert result.stdout.startswith("%d rules\n" % len(RULE_NAMES))
    for name in RULE_NAMES:
        assert name in result.stdout
    # The promise that makes this output safe to paste anywhere.
    for name in RULE_NAMES:
        assert needle_for(name) not in result.stdout


def test_list_rules_needs_no_path():
    result = run_tool("--list-rules", cwd="/")
    assert result.returncode == scanner.EXIT_CLEAN


# ------------------------------------------------------------ the real repo


def test_the_repository_scans_clean():
    """The scanner has to survive its own rules, including this test file."""
    result = run_tool(".", "--json")
    assert result.returncode == scanner.EXIT_CLEAN, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["hits"] == 0
    # A scan that walked nothing would also report zero hits.
    assert payload["files_scanned"] > 40
    assert payload["findings"] == []


def test_the_scanner_is_itself_clean():
    assert scanner.scan_tree(TOOL_PATH) == []


def test_the_test_suite_is_scanned_too():
    """The suite is the newest and largest body of text in the repository, and
    the most likely place for a copied transcript to end up."""
    findings = scanner.scan_tree(os.path.join(REPO_ROOT, "tests"))
    assert findings == []


def test_the_fixtures_carry_no_real_session_data():
    """The fixtures are synthetic by construction; this states the property.

    A parser fixture is exactly where a real transcript would be pasted "just to
    reproduce the bug", and it is the one artefact nobody re-reads.
    """
    fixture_dir = os.path.join(REPO_ROOT, "tests", "fixtures")
    assert os.path.isdir(fixture_dir)
    names = sorted(os.listdir(fixture_dir))
    assert names, "fixtures are missing"
    findings = scanner.scan_tree(fixture_dir)
    assert findings == []
    for name in names:
        path = os.path.join(fixture_dir, name)
        if not name.endswith(".jsonl"):
            continue
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                # Every record has to parse as JSON, which a hand-edited
                # transcript excerpt pasted out of context would not.
                assert isinstance(json.loads(line), dict), name
