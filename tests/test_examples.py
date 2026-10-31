"""The shipped example files, and the installer that seeds them.

Two things here are easy to get wrong and invisible until a user hits them:

1. ``config.example.json`` is generated from ``config.example.yaml`` (PyYAML is
   optional, and a stock ``python3`` does not have it). Two hand-maintained
   copies would drift, and the drift would only show up as "the tool ignores my
   settings" on a machine without PyYAML.
2. The installer picks the config extension from the interpreter it installs
   against. Seeding ``config.yaml`` for an interpreter that cannot read YAML
   installs a tool that refuses to start -- which is what happened the first
   time the acceptance run installed into a bare sandbox ``python3``.

Both are checked against the real files in the repository, not fixtures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from custos.core.gate import load_questions, validate_bank
from custos.core.util import have_yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(REPO_ROOT, "config")
CONFIG_YAML = os.path.join(CONFIG_DIR, "config.example.yaml")
CONFIG_JSON = os.path.join(CONFIG_DIR, "config.example.json")
QUESTIONS_YAML = os.path.join(CONFIG_DIR, "questions.example.yaml")
QUESTIONS_JSON = os.path.join(CONFIG_DIR, "questions.example.json")
INSTALLER = os.path.join(REPO_ROOT, "install.sh")

needs_yaml = pytest.mark.skipif(
    not have_yaml(), reason="PyYAML is not installed in this interpreter")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_yaml(path):
    import yaml  # only reached when have_yaml() is true
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ------------------------------------------------------------------- examples


def test_both_config_examples_exist():
    assert os.path.isfile(CONFIG_YAML), CONFIG_YAML
    assert os.path.isfile(CONFIG_JSON), CONFIG_JSON
    assert os.path.getsize(CONFIG_JSON) > 0


@needs_yaml
def test_the_json_config_mirrors_the_yaml_config():
    """Same settings, one difference: the bank each one names.

    The YAML example points at ``questions.yaml`` and the JSON one at
    ``questions.json``, because the file a JSON install seeds is the JSON bank.
    Everything else has to match, key for key, or the JSON install silently
    runs different defaults.
    """
    yaml_data = read_yaml(CONFIG_YAML)
    json_data = read_json(CONFIG_JSON)

    assert json_data["gate"]["questions"] == "questions.json"
    assert yaml_data["gate"]["questions"] == "questions.yaml"
    yaml_data["gate"]["questions"] = "questions.json"

    assert json_data == yaml_data


@needs_yaml
def test_the_json_questions_mirror_the_yaml_questions():
    assert read_json(QUESTIONS_JSON) == read_yaml(QUESTIONS_YAML)


def test_the_json_bank_is_valid_and_has_varied_graders():
    """A shipped bank that fails its own validator would be a bad first
    impression, and a bank of one grader demonstrates one grader."""
    report = validate_bank(QUESTIONS_JSON)
    assert report["ok"], report["problems"]
    questions = load_questions(QUESTIONS_JSON)
    assert len(questions) >= 5
    assert len({question.match for question in questions}) >= 3


@needs_yaml
def test_the_yaml_bank_is_valid_too():
    report = validate_bank(QUESTIONS_YAML)
    assert report["ok"], report["problems"]


def test_the_yaml_example_carries_the_commentary():
    """The JSON file cannot hold comments, so the YAML one has to keep them:
    it is the document a user reads when deciding what to change."""
    with open(CONFIG_YAML, encoding="utf-8") as handle:
        text = handle.read()
    assert "#" in text
    assert text.count("\n#") >= 20


# ------------------------------------------------------------------ installer


def interpreter_reads_yaml():
    """Whether the interpreter running this suite can read YAML."""
    result = subprocess.run(
        [sys.executable, "-c", "import yaml"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def install(target, home):
    """Run the installer into ``target`` with a sandboxed HOME."""
    env = dict(os.environ)
    env["HOME"] = home
    env["CUSTOS_PYTHON"] = sys.executable
    env.pop("CUSTOS_CONFIG_DIR", None)
    return subprocess.run(
        ["bash", INSTALLER, "--yes", "--no-daemon", "--prefix", target],
        cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, timeout=300,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_the_installer_seeds_a_config_this_interpreter_can_read(tmp_path):
    target = str(tmp_path / "prefix")
    home = str(tmp_path / "home")
    os.makedirs(home)

    result = install(target, home)
    assert result.returncode == 0, result.stdout

    config_dir = os.path.join(home, ".config", "custos")
    seeded = sorted(os.path.join(config_dir, name)
                    for name in os.listdir(config_dir))
    assert seeded, "the installer seeded nothing"
    names = [os.path.basename(path) for path in seeded]

    expects_yaml = interpreter_reads_yaml()
    extension = ".yaml" if expects_yaml else ".json"
    assert names == ["config%s" % extension, "questions%s" % extension], names
    if not expects_yaml:
        # The reason the JSON pair exists at all.
        assert not any(name.endswith(".yaml") for name in names)

    # The proof: the installed entry point runs against what was seeded.
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("CUSTOS_CONFIG_DIR", None)
    doctor = subprocess.run(
        [os.path.join(target, "bin", "custos"), "doctor"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, timeout=120,
    )
    assert doctor.returncode == 0, doctor.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")
def test_the_installer_keeps_an_existing_config(tmp_path):
    """An upgrade must not overwrite the settings a user has edited."""
    target = str(tmp_path / "prefix")
    home = str(tmp_path / "home")
    config_dir = os.path.join(home, ".config", "custos")
    os.makedirs(config_dir)

    first = install(target, home)
    assert first.returncode == 0, first.stdout
    seeded = sorted(os.listdir(config_dir))[0]
    path = os.path.join(config_dir, seeded)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"version": 1, "monitor": {"interval_minutes": 99}}\n')

    second = install(target, home)
    assert second.returncode == 0, second.stdout
    with open(path, encoding="utf-8") as handle:
        assert "99" in handle.read()
    assert "kept existing" in second.stdout
