"""First-run interactive setup for the android-app-debug MCP skill.

Writes host-level defaults to ``~/.config/android-app-debug/config.json``
(mode 0600):

- Android SDK path
- Default AVD name

Project-specific values (``project_root``, ``package``, ``gradle_task``,
…) are passed at invocation time via the ``start_debug_session`` tool, not
stored here. Re-running edits the existing config in-place.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from android_ops import android_sdk_root  # noqa: E402
from emulator import list_avds  # noqa: E402
from paths import config_path, ensure_config_dir  # noqa: E402


CONFIG_PATH = config_path()


def _prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError:
            return default or ""
        if value:
            return value
        if default is not None:
            return default


def _prompt_choice(label: str, options: List[str], default_index: int = 0) -> str:
    print(label)
    for i, opt in enumerate(options, 1):
        marker = "*" if i - 1 == default_index else " "
        print(f"  {marker} {i}. {opt}")
    raw = _prompt(f"  pick 1-{len(options)}", default=str(default_index + 1))
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        pass
    return options[default_index]


def _load_existing() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            print(f"warning: existing config at {CONFIG_PATH} is not valid JSON, starting fresh")
    return {}


def _save(config: Dict[str, Any]) -> None:
    ensure_config_dir()
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def main() -> int:
    print("== android-app-debug (MCP) onboarding ==\n")
    config = _load_existing()

    # ── Android SDK ──
    existing_sdk = config.get("android_sdk_path")
    detected = str(android_sdk_root())
    sdk = _prompt("Android SDK path", default=existing_sdk or detected)
    config["android_sdk_path"] = sdk

    # ── AVD ──
    avds = list_avds(Path(os.path.expanduser(sdk)))
    if avds:
        existing_avd = config.get("default_avd_name")
        default_idx = avds.index(existing_avd) if existing_avd in avds else 0
        config["default_avd_name"] = _prompt_choice(
            "Default AVD (used when no `avd` arg is passed to start_debug_session):",
            avds,
            default_index=default_idx,
        )
    else:
        print(
            "no AVDs found via `emulator -list-avds`. "
            "Create one with Android Studio's AVD Manager, then re-run onboarding."
        )
        config["default_avd_name"] = config.get("default_avd_name", "")

    config.pop("llm", None)
    config.pop("max_steps_default", None)

    _save(config)
    print(f"\nwrote {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
