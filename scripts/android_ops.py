"""
adb wrappers used by the marvis_dev agent and the debug_run orchestrator.

All commands shell out via subprocess; no `adbutils` / `pure-python-adb` dependency
because we want to match exactly what `test-android-app` does and keep the skill
dependency-light.

ADB and emulator binaries are resolved via the ANDROID_SDK_ROOT or ANDROID_HOME
env vars, falling back to ~/Library/Android/sdk on macOS.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ── SDK / binary discovery ──────────────────────────────────────────

def android_sdk_root(override: Optional[str] = None) -> Path:
    if override:
        return Path(os.path.expanduser(override))
    env = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~/Library/Android/sdk"))


def adb_bin(sdk_root: Optional[Path] = None) -> str:
    sdk = sdk_root or android_sdk_root()
    candidate = sdk / "platform-tools" / "adb"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("adb")
    if found:
        return found
    raise FileNotFoundError(f"adb not found at {candidate} or on PATH")


def emulator_bin(sdk_root: Optional[Path] = None) -> str:
    sdk = sdk_root or android_sdk_root()
    candidate = sdk / "emulator" / "emulator"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("emulator")
    if found:
        return found
    raise FileNotFoundError(f"emulator not found at {candidate} or on PATH")


# ── Process helpers ─────────────────────────────────────────────────

class CommandError(RuntimeError):
    def __init__(self, cmd: List[str], code: int, stdout: str, stderr: str):
        super().__init__(
            f"command failed (exit {code}): {' '.join(shlex.quote(c) for c in cmd)}\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
        self.cmd = cmd
        self.code = code
        self.stdout = stdout
        self.stderr = stderr


def run(cmd: List[str], timeout: int = 60, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing text stdout/stderr."""
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, **kwargs,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def run_binary(cmd: List[str], timeout: int = 60, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess and capture binary stdout (for screenshots, etc.)."""
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, **kwargs,
    )
    if check and proc.returncode != 0:
        raise CommandError(
            cmd, proc.returncode,
            proc.stdout[:200].decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
    return proc


# ── ADB device gateway ──────────────────────────────────────────────

class Adb:
    """Thin handle for one device."""

    def __init__(self, serial: Optional[str] = None, sdk_root: Optional[Path] = None):
        self.serial = serial
        self._adb = adb_bin(sdk_root)

    @property
    def cmd_prefix(self) -> List[str]:
        if self.serial:
            return [self._adb, "-s", self.serial]
        return [self._adb]

    def shell(self, args: str | List[str], timeout: int = 30) -> str:
        if isinstance(args, str):
            cmd = self.cmd_prefix + ["shell", args]
        else:
            cmd = self.cmd_prefix + ["shell", *args]
        return run(cmd, timeout=timeout).stdout

    def shell_binary(self, args: str | List[str], timeout: int = 30) -> bytes:
        if isinstance(args, str):
            cmd = self.cmd_prefix + ["shell", args]
        else:
            cmd = self.cmd_prefix + ["shell", *args]
        return run_binary(cmd, timeout=timeout).stdout

    def exec_out(self, args: str | List[str], timeout: int = 30) -> bytes:
        if isinstance(args, str):
            cmd = self.cmd_prefix + ["exec-out", args]
        else:
            cmd = self.cmd_prefix + ["exec-out", *args]
        return run_binary(cmd, timeout=timeout).stdout

    def run_cmd(self, args: List[str], timeout: int = 60) -> str:
        return run(self.cmd_prefix + args, timeout=timeout).stdout

    # ── Screen ────────────────────────────────────────────────────

    def screen_size(self) -> Tuple[int, int]:
        """Return device screen size in *device* pixels (width, height)."""
        out = self.shell("wm size")
        # Output: "Physical size: 1080x2400" possibly followed by "Override size: ..."
        m = re.search(r"(?:Override|Physical) size:\s*(\d+)x(\d+)", out)
        if not m:
            raise RuntimeError(f"could not parse screen size from: {out!r}")
        return int(m.group(1)), int(m.group(2))

    def screencap_b64(self) -> str:
        png = self.exec_out("screencap -p")
        return base64.b64encode(png).decode("ascii")

    def dump_ui_tree(self) -> str:
        """Run uiautomator dump, return XML string."""
        # First try the streaming form (--exec-mode=stdout) where supported, else file.
        out = self.shell("uiautomator dump /sdcard/window_dump.xml", timeout=30)
        if "ERROR" in out and "dumped" not in out.lower():
            raise RuntimeError(f"uiautomator dump failed: {out!r}")
        return self.shell("cat /sdcard/window_dump.xml", timeout=30)

    # ── Input ─────────────────────────────────────────────────────

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        x = int(x); y = int(y)
        self.shell(f"input swipe {x} {y} {x} {y} {int(duration_ms)}")

    def swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int = 300) -> None:
        self.shell(f"input touchscreen swipe {int(x0)} {int(y0)} {int(x1)} {int(y1)} {int(duration_ms)}")

    def keyevent(self, code: int) -> None:
        self.shell(f"input keyevent {int(code)}")

    def type_text(self, text: str) -> None:
        # `input text` requires spaces escaped as %s; backslash-escape literal %s.
        encoded = text.replace("%s", "\\%s").replace(" ", "%s")
        # Safest: pass via stdin to avoid shell quoting horror.
        self.shell(f"input text {shlex.quote(encoded)}")

    # ── App lifecycle ─────────────────────────────────────────────

    def install(self, apk_path: Path) -> None:
        self.run_cmd(["install", "-r", str(apk_path)], timeout=180)

    def force_stop(self, package: str) -> None:
        try:
            self.shell(f"am force-stop {shlex.quote(package)}")
        except CommandError:
            pass

    def launch(self, package: str) -> None:
        # Try monkey first (simplest, doesn't require knowing the activity)
        try:
            self.shell(
                f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1"
            )
            return
        except CommandError:
            pass

        # Fallback: resolve launcher activity and start it explicitly
        try:
            resolved = self.shell(
                f"cmd package resolve-activity --brief {shlex.quote(package)}"
            )
            # The last non-empty line is "package/activity"
            line = next(
                (ln.strip() for ln in resolved.strip().splitlines()[::-1] if ln.strip()),
                None,
            )
            if line and "/" in line:
                self.shell(f"am start -n {shlex.quote(line)}")
                return
        except CommandError:
            pass

        raise RuntimeError(f"could not launch {package!r} via monkey or am start")

    def pid_of(self, package: str) -> Optional[int]:
        try:
            out = self.shell(f"pidof -s {shlex.quote(package)}").strip()
            return int(out) if out else None
        except (CommandError, ValueError):
            return None

    def wait_for_pid(self, package: str, timeout: float = 10.0) -> Optional[int]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            pid = self.pid_of(package)
            if pid:
                return pid
            time.sleep(0.3)
        return None

    # ── Device list ───────────────────────────────────────────────

    def devices(self) -> List[Tuple[str, str]]:
        """Return [(serial, state), ...]."""
        out = run([self._adb, "devices"]).stdout
        result = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                result.append((parts[0], parts[1]))
        return result


# ── Build ───────────────────────────────────────────────────────────

def find_gradlew(start: Path) -> Path:
    """Walk up from `start` looking for a gradlew script."""
    cur = start.resolve()
    for _ in range(8):
        candidate = cur / "gradlew"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    raise FileNotFoundError(f"no gradlew found at or above {start}")


def build_apk(project_root: Path, gradle_task: str = "assembleDebug", timeout: int = 600) -> Path:
    """
    Run `./gradlew <gradle_task>` and return the path to the produced debug APK.

    Surfaces gradle stderr verbatim on failure so the caller can show it as the
    "first check" output to the developer.
    """
    project_root = Path(project_root).resolve()
    gradlew = find_gradlew(project_root)

    proc = subprocess.run(
        [str(gradlew), gradle_task],
        cwd=str(gradlew.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise CommandError(
            [str(gradlew), gradle_task], proc.returncode, proc.stdout, proc.stderr,
        )

    # Find the produced APK. Prefer one whose path matches the gradle task.
    candidates = sorted(
        project_root.glob("**/build/outputs/apk/**/*-debug.apk"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        # Fallback: any APK newly produced under build/outputs/apk/
        candidates = sorted(
            project_root.glob("**/build/outputs/apk/**/*.apk"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not candidates:
        raise FileNotFoundError(
            f"build succeeded but no APK found under {project_root}/**/build/outputs/apk/"
        )

    flavor_hint = re.sub(r"^assemble", "", gradle_task).lower()
    for c in candidates:
        if flavor_hint and flavor_hint in str(c).lower():
            return c
    return candidates[0]


# ── Higher-level scrolling ──────────────────────────────────────────

def screen_to_swipe_coords(
    width: int, height: int, direction: str
) -> Tuple[int, int, int, int]:
    cx = width // 2
    cy = height // 2
    dx = width // 4
    dy = height // 4
    if direction == "down":
        return cx, cy + dy, cx, cy - dy
    if direction == "up":
        return cx, cy - dy, cx, cy + dy
    if direction == "left":
        return cx + dx, cy, cx - dx, cy
    if direction == "right":
        return cx - dx, cy, cx + dx, cy
    raise ValueError(f"unknown scroll direction: {direction!r}")


def scroll(adb: Adb, direction: str, screen_size: Tuple[int, int],
           x: Optional[int] = None, y: Optional[int] = None) -> None:
    w, h = screen_size
    if x is not None and y is not None:
        # Localized scroll around (x, y)
        dx = w // 6
        dy = h // 6
        if direction == "down":
            adb.swipe(x, y + dy, x, y - dy, 300)
        elif direction == "up":
            adb.swipe(x, y - dy, x, y + dy, 300)
        elif direction == "left":
            adb.swipe(x + dx, y, x - dx, y, 300)
        elif direction == "right":
            adb.swipe(x - dx, y, x + dx, y, 300)
        else:
            raise ValueError(f"unknown scroll direction: {direction!r}")
    else:
        adb.swipe(*screen_to_swipe_coords(w, h, direction), 300)


def scroll_to_text(
    adb: Adb,
    needle: str,
    screen_size: Tuple[int, int],
    direction: str = "down",
    max_scrolls: int = 8,
) -> bool:
    """Scroll until UI tree contains `needle` (case-insensitive). Best-effort."""
    needle_low = needle.lower()
    for _ in range(max_scrolls):
        try:
            xml = adb.dump_ui_tree()
        except Exception:
            xml = ""
        if needle_low in xml.lower():
            return True
        scroll(adb, direction, screen_size)
        time.sleep(1.0)
    try:
        xml = adb.dump_ui_tree()
    except Exception:
        xml = ""
    return needle_low in xml.lower()
