"""Configuration loading and path resolution.

The configuration directory is resolved once and every other path in the tool is
derived from it, so a caller can point the whole tool at a sandbox by setting
``CUSTOS_CONFIG_DIR``.

YAML is supported only when PyYAML happens to be installed; the JSON path always
works and is what the tool falls back to. Missing keys are filled in from
:data:`DEFAULT_CONFIG`, so a partial config file is valid.
"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Optional

from .util import CustosError, have_yaml, log, read_json

CONFIG_ENV_VAR = "CUSTOS_CONFIG_DIR"
DEFAULT_CONFIG_DIRNAME = os.path.join(".config", "custos")

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "adapters": {
        "claude_code": {
            "enabled": True,
            "projects_dir": "~/.claude/projects",
            "window": 200000,
        },
        "codex": {
            "enabled": True,
            "sessions_dir": "~/.codex/sessions",
            "archived_dir": "~/.codex/archived_sessions",
            "window": 200000,
        },
        "opencode": {
            "enabled": True,
            "db_path": "~/.local/share/opencode/opencode.db",
            "storage_dir": "~/.local/share/opencode/storage",
            "window": 200000,
        },
        "pi": {
            "enabled": True,
            "sessions_dir": "~/.pi/agent/sessions",
            "window": 200000,
        },
        "generic": {
            "enabled": False,
            "sessions": [],
        },
    },
    "thresholds": {
        # Absolute token counts. Set to null to use the percent form instead.
        "baseline_tokens": 350000,
        "baseline_percent": None,
        "warn_factor": 0.90,
        "rotate_factor": 0.93,
    },
    "monitor": {
        "interval_minutes": 5,
        "activity_quiet_minutes": 10,
        "fire_budget_seconds": 900,
        "min_retry_gap_seconds": 120,
        "pending_max_age_days": 3.0,
    },
    "rotation": {
        # null means "<config dir>/handovers". An absolute path is allowed and
        # is what the example config shows, but the relative default is what
        # keeps a sandboxed config dir a sandbox.
        "handover_dir": None,
        "mode": "assisted",
        "modes": {},
        "idempotency_hours": 6.0,
    },
    "gate": {
        "enabled": True,
        "questions": "questions.yaml",
        "mode": "random",
        "attempt_limit": 5,
        "attempt_interval_seconds": 60,
        "timeout_seconds": 240,
        "prefix": (
            "Do not use any tools. Do not read any files. "
            "Answer with the final answer only."
        ),
        "alert_after_failures": 5,
        "alert_after_invalid": 5,
        "alert_repeat_seconds": 3600,
    },
    "notify": {
        "stdout": True,
        "webhook_url": None,
        "webhook_timeout_seconds": 10,
    },
    "state": {
        "ledger_max_age_days": 45,
        "keep_readings_days": 7,
    },
}


def expand(path: Optional[str]) -> Optional[str]:
    """Expand ``~`` and environment variables in a configured path."""
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(path))


def config_dir(explicit: Optional[str] = None) -> str:
    """Resolve the configuration directory.

    Precedence: explicit argument, ``$CUSTOS_CONFIG_DIR``, then
    ``~/.config/custos``. ``$HOME`` is honoured, so the tool can be sandboxed
    by pointing ``HOME`` at a scratch directory.
    """
    if explicit:
        return os.path.abspath(expand(explicit))
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return os.path.abspath(expand(from_env))
    return os.path.join(os.path.expanduser("~"), DEFAULT_CONFIG_DIRNAME)


class Config:
    """Loaded configuration plus the paths derived from its directory."""

    def __init__(self, directory: str, data: Dict[str, Any], source: Optional[str]) -> None:
        self.dir = directory
        self.data = data
        self.source = source

    # -- path helpers ----------------------------------------------------
    def path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    @property
    def state_path(self) -> str:
        return self.path("state.json")

    @property
    def events_path(self) -> str:
        return self.path("events.jsonl")

    @property
    def readings_dir(self) -> str:
        return self.path("readings")

    @property
    def disable_path(self) -> str:
        return self.path("DISABLE")

    @property
    def lock_path(self) -> str:
        return self.path("custos.lock")

    @property
    def questions_path(self) -> str:
        """Absolute path to the question bank.

        Relative values are resolved inside the config directory so a bank ships
        alongside its config.
        """
        configured = self.get("gate", "questions", default="questions.yaml")
        resolved = expand(str(configured))
        return resolved if os.path.isabs(resolved) else self.path(str(configured))

    @property
    def handover_dir(self) -> str:
        """Where snapshots are written.

        A configured value is used as given; the default is ``handovers`` inside
        the configuration directory. Deriving the default from ``self.dir``
        rather than hard-coding ``~/.config`` is what lets a caller point
        ``CUSTOS_CONFIG_DIR`` (or ``HOME``) at a temporary directory and have
        the whole tool follow, instead of writing snapshots into the real user's
        home while the rest of the run is sandboxed.
        """
        configured = self.get("rotation", "handover_dir", default=None)
        if configured:
            resolved = expand(str(configured))
            return resolved if os.path.isabs(resolved) else self.path(resolved)
        return self.path("handovers")

    @property
    def disabled(self) -> bool:
        """Whether the kill-switch file exists."""
        return os.path.exists(self.disable_path)

    # -- accessors -------------------------------------------------------
    def section(self, name: str) -> Dict[str, Any]:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """One setting, with ``null`` treated as "not set".

        A key that is present but ``null`` falls back to the default rather than
        to ``None``: a config file written by hand often leaves a key in place
        with a null value to say "I am not overriding this", and reading that as
        an explicit empty value would silently disable a feature.
        """
        value = self.section(section).get(key, default)
        return default if value is None and default is not None else value

    def get_int(self, section: str, key: str, default: int) -> int:
        """Integer setting, falling back on anything unparseable."""
        try:
            return int(self.get(section, key, default=default))
        except (TypeError, ValueError):
            return default

    def get_float(self, section: str, key: str, default: float) -> float:
        try:
            return float(self.get(section, key, default=default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, section: str, key: str, default: bool) -> bool:
        value = self.get(section, key, default=default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def adapter(self, name: str) -> Dict[str, Any]:
        adapters = self.section("adapters")
        value = adapters.get(name)
        return value if isinstance(value, dict) else {}

    def adapter_enabled(self, name: str) -> bool:
        return bool(self.adapter(name).get("enabled", False))

    def adapter_window(self, name: str, fallback: int = 200000) -> int:
        """Configured context window for an adapter, in tokens."""
        value = self.adapter(name).get("window")
        try:
            window = int(value)
        except (TypeError, ValueError):
            return fallback
        return window if window > 0 else fallback


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``.

    Lists are replaced rather than concatenated: a configured adapter list is a
    complete statement of intent, not an addition to the defaults.
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(config_dir_path: Optional[str] = None) -> Config:
    """Load configuration, merging whatever exists over the defaults.

    A missing config directory is not an error: the tool runs on defaults so
    ``scan`` works before any setup has happened.
    """
    directory = config_dir(config_dir_path)
    yaml_path = os.path.join(directory, "config.yaml")
    json_path = os.path.join(directory, "config.json")

    data: Dict[str, Any] = {}
    source: Optional[str] = None

    if os.path.exists(yaml_path):
        data = _load_yaml_file(yaml_path)
        source = yaml_path
    elif os.path.exists(json_path):
        data = read_json(json_path, default={}) or {}
        if not isinstance(data, dict):
            raise CustosError("%s must contain a JSON object" % json_path)
        source = json_path

    merged = deep_merge(DEFAULT_CONFIG, data)
    return Config(directory, merged, source)


def _load_yaml_file(path: str) -> Dict[str, Any]:
    """Load a YAML config file, with an actionable error when PyYAML is absent."""
    if not have_yaml():
        raise CustosError(
            "%s is YAML but PyYAML is not installed. Install it with "
            "`pip install custos[yaml]`, or convert the file to config.json "
            "(same keys, JSON syntax)." % path
        )
    import yaml  # type: ignore

    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except OSError as exc:
        raise CustosError("cannot read %s: %s" % (path, exc))
    except Exception as exc:  # yaml.YAMLError and friends
        raise CustosError("cannot parse %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise CustosError("%s must contain a mapping at the top level" % path)
    return data


def warn_unused_yaml(default_dir: str) -> None:
    """Explain why a ``config.yaml`` sitting in the config dir was ignored."""
    yaml_path = os.path.join(default_dir, "config.yaml")
    if os.path.exists(yaml_path) and not have_yaml():
        log(
            "[warn] %s found but PyYAML is unavailable; using config.json or "
            "built-in defaults instead." % yaml_path
        )


def adapter_names() -> List[str]:
    """Adapter names in the order they should be presented."""
    return ["claude_code", "codex", "opencode", "pi", "generic"]
