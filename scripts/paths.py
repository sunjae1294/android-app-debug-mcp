"""Shared filesystem paths for the android-app-debug skill.

Config and run artifacts live under the user's XDG dirs so the skill works
when installed read-only as a Claude Code plugin. The skill directory itself
is never written to.

Environment overrides (so power users and CI can relocate state):

- ``ANDROID_APP_DEBUG_CONFIG`` — full path to the config.json file.
- ``ANDROID_APP_DEBUG_RUNS_DIR`` — directory under which per-run subdirs land.

Otherwise we honor ``XDG_CONFIG_HOME`` / ``XDG_CACHE_HOME`` if set, else fall
back to ``~/.config`` / ``~/.cache``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


def _xdg_dir(env_var: str, default_subpath: str) -> Path:
    value = os.environ.get(env_var, "").strip()
    if value:
        return Path(os.path.expanduser(value))
    return Path(os.path.expanduser("~")) / default_subpath


def config_path() -> Path:
    override = os.environ.get("ANDROID_APP_DEBUG_CONFIG", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "android-app-debug" / "config.json"


def runs_root() -> Path:
    override = os.environ.get("ANDROID_APP_DEBUG_RUNS_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return _xdg_dir("XDG_CACHE_HOME", ".cache") / "android-app-debug" / "runs"


def load_config() -> Dict[str, Any]:
    """Read and parse config.json. Returns {} if absent.

    Prints a stderr warning (but does not raise) if the file exists but is
    not valid JSON — otherwise a typo silently masquerades as a missing
    config and the user gets a confusing "no model specified" error later.
    """
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"[config] {path}: invalid JSON ({e}); ignoring", file=sys.stderr)
        return {}
    except OSError as e:
        print(f"[config] {path}: could not read ({e}); ignoring", file=sys.stderr)
        return {}


def ensure_config_dir() -> Path:
    """Make sure the config dir exists with mode 0700 and return it."""
    d = config_path().parent
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d
