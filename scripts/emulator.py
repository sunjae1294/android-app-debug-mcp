"""
AVD lifecycle helpers.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from android_ops import adb_bin, emulator_bin, run, Adb


def list_avds(sdk_root: Optional[Path] = None) -> List[str]:
    try:
        emu = emulator_bin(sdk_root)
    except FileNotFoundError:
        return []
    proc = run([emu, "-list-avds"], timeout=15, check=False)
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def is_running(serial: Optional[str] = None, sdk_root: Optional[Path] = None) -> bool:
    """Return True if at least one emulator is in `device` state."""
    proc = run([adb_bin(sdk_root), "devices"], timeout=15, check=False)
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            if serial is None or parts[0] == serial:
                return True
    return False


def start_avd(name: str, sdk_root: Optional[Path] = None,
              extra_args: Optional[List[str]] = None) -> subprocess.Popen:
    """Spawn the AVD detached. Returns the Popen handle."""
    emu = emulator_bin(sdk_root)
    args = [emu, "-avd", name, "-no-snapshot-save"]
    if extra_args:
        args.extend(extra_args)
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_for_boot(serial: Optional[str] = None, sdk_root: Optional[Path] = None,
                  timeout: int = 240) -> None:
    """Block until `sys.boot_completed == 1` and adb shows the device."""
    deadline = time.time() + timeout
    adb = Adb(serial=serial, sdk_root=sdk_root)

    # Wait for adb to see the device first.
    while time.time() < deadline:
        if is_running(serial, sdk_root):
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"emulator did not appear in `adb devices` within {timeout}s")

    # Then wait for boot_completed.
    while time.time() < deadline:
        try:
            out = adb.shell("getprop sys.boot_completed").strip()
            if out == "1":
                # Dismiss lock screen (best effort)
                try:
                    adb.keyevent(82)
                except Exception:
                    pass
                # Tiny settle so the launcher is fully drawn before we drive it.
                time.sleep(2)
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"emulator did not finish booting within {timeout}s")


def ensure_running(name: Optional[str], sdk_root: Optional[Path] = None,
                   timeout: int = 240) -> None:
    """If no emulator is running, start `name` and wait for it. No-op otherwise.

    The emulator is started with a visible window by default (no `-no-window`).
    We recommend keeping the visible mode so you can watch the agent navigate;
    use `extra_args=["-no-window"]` via `start_avd` only when you explicitly
    need headless (e.g., CI).
    """
    if is_running(sdk_root=sdk_root):
        return
    available = list_avds(sdk_root)
    if not available:
        raise RuntimeError(
            "no AVDs are installed on this machine. Create one before running "
            "the skill: open Android Studio → Tools → Device Manager → Create "
            "Device, or run "
            "`~/Library/Android/sdk/cmdline-tools/latest/bin/avdmanager create avd ...` "
            "from the CLI. Then re-run with `--avd <name>` or run "
            "`scripts/onboard.py` to set a default."
        )
    if not name:
        raise RuntimeError(
            "no emulator running and no AVD name provided. "
            f"Available AVDs: {available}. "
            "Pass `--avd <name>` or run `scripts/onboard.py` to set a default."
        )
    if name not in available:
        raise RuntimeError(
            f"AVD {name!r} not found. Available: {available}"
        )
    start_avd(name, sdk_root)
    wait_for_boot(sdk_root=sdk_root, timeout=timeout)


def list_attached_emulators(sdk_root: Optional[Path] = None) -> List[str]:
    """Return serials of all *emulators* in `device` state, excluding real devices."""
    from android_ops import adb_bin
    proc = run([adb_bin(sdk_root), "devices"], timeout=15, check=False)
    serials: List[str] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and parts[0].startswith("emulator-"):
            serials.append(parts[0])
    return serials


def list_attached_devices(sdk_root: Optional[Path] = None) -> List[Tuple[str, str]]:
    """Return (serial, kind) for everything in `device` state — kind is 'emulator' or 'device'."""
    from android_ops import adb_bin
    proc = run([adb_bin(sdk_root), "devices"], timeout=15, check=False)
    out: List[Tuple[str, str]] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            kind = "emulator" if parts[0].startswith("emulator-") else "device"
            out.append((parts[0], kind))
    return out
