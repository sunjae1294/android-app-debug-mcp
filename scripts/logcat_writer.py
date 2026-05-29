"""
LogcatWriter: backgrounded `adb logcat -v time --pid=N >> log_path`, with a
thread-safe channel for inserting inline `<<< MARVIS_DEV ... >>>` markers
*into the same file* so the developer gets a unified timeline.

POSIX append-mode writes <= PIPE_BUF (4 KB) are atomic, which our marker line
fits comfortably inside. The lock is just for in-process ordering.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from android_ops import Adb


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def format_action(action: dict) -> str:
    """Render an action JSON as a compact one-liner: `name(k=v, k=v)`.

    Used both for inline logcat markers and for the per-step stdout line
    printed by marvis_dev — keep the two surfaces visually identical.
    """
    name = action.get("name", "?")
    params = action.get("parameters", {}) or {}
    parts = []
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 60:
            v = v[:57] + "..."
        if isinstance(v, str):
            v = v.replace("\n", " ").replace("\r", " ")
            parts.append(f'{k}="{v}"')
        else:
            parts.append(f"{k}={v}")
    return f"{name}({', '.join(parts)})"


_format_action = format_action  # internal alias retained for clarity below


class LogcatWriter:
    """
    Run `adb logcat` in the background filtered to one app's PID, write to
    `log_path`, and expose a thread-safe `write_marker(action, summary)`.
    """

    def __init__(self, adb: Adb, package: str, log_path: Path,
                 extra_filters: Optional[List[str]] = None):
        self.adb = adb
        self.package = package
        self.log_path = Path(log_path)
        self.extra_filters = extra_filters or []
        self._proc: Optional[subprocess.Popen] = None
        self._fp = None
        self._lock = threading.Lock()
        self._pid: Optional[int] = None

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, pid_wait_timeout: float = 15.0) -> None:
        # Clear logcat buffers so the run starts from a clean slate.
        try:
            self.adb.run_cmd(["logcat", "-c"], timeout=10)
        except Exception:
            pass

        # Resolve the app's PID. If the app hasn't started yet, fall back to
        # filtering by tag/process name via `--pid` once available, so we may
        # need to wait briefly.
        pid = self.adb.wait_for_pid(self.package, timeout=pid_wait_timeout)
        self._pid = pid

        # Open the log file for our own marker writes (separate fd from logcat).
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.log_path, "a", buffering=1, encoding="utf-8")

        # Header
        header = (
            f"# logcat for {self.package}"
            + (f" (pid={pid})" if pid else " (pid=unresolved-at-start)")
            + f" started {datetime.now().isoformat(timespec='seconds')}\n"
            f"# Inline `<<< MARVIS_DEV ... >>>` markers below were inserted by "
            f"the marvis_dev agent at the moment each action fired.\n"
        )
        self._write_raw(header)

        # Spawn logcat. Append mode; logcat writes its own line-buffered output.
        cmd = self.adb.cmd_prefix + ["logcat", "-v", "time"]
        if pid:
            cmd += [f"--pid={pid}"]
        cmd += self.extra_filters

        log_fp = open(self.log_path, "a")
        self._proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # The Popen owns the file handle; once Popen exits the OS releases it.
        # We keep our own self._fp for marker writes.

    def stop(self, capture_crash_buffer: bool = True) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                # SIGTERM the whole process group to ensure adb's child is killed too.
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        # Capture any crashes that happened during the run (logcat -b crash -d).
        if capture_crash_buffer:
            try:
                crash = self.adb.run_cmd(["logcat", "-b", "crash", "-d"], timeout=10)
                if crash.strip():
                    self._write_raw(
                        "\n# === crash buffer snapshot at run end ===\n"
                        + crash
                        + "# === end crash buffer ===\n"
                    )
            except Exception:
                pass

        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    # ── Markers ───────────────────────────────────────────────────

    def write_marker(self, action: dict, summary: str, ux_feedback: str = "") -> Tuple[str, str]:
        """
        Write an inline marker for an action that is about to fire.

        When `ux_feedback` is non-empty it is appended as ` [ux: <text>]` so a
        developer reading the timeline sees the friction note next to the action
        that surfaced it. The feedback is single-lined and truncated to keep
        the whole marker comfortably below POSIX PIPE_BUF (4 KB) so the append
        stays atomic.

        Returns (timestamp, formatted_action) so callers can use them in JSONL.
        """
        ts = _ts()
        formatted = _format_action(action)
        suffix = ""
        if ux_feedback:
            trimmed = ux_feedback.replace("\n", " ").replace("\r", " ").strip()
            if len(trimmed) > 240:
                trimmed = trimmed[:237] + "..."
            if trimmed:
                suffix = f" [ux: {trimmed}]"
        line = f"<<< MARVIS_DEV {ts} {formatted} — {summary}{suffix} >>>\n"
        with self._lock:
            self._write_raw(line)
        return ts, formatted

    def write_note(self, note: str) -> None:
        """Free-form note (e.g., "step 3 starting", "max_steps reached")."""
        ts = _ts()
        line = f"<<< MARVIS_DEV {ts} NOTE — {note} >>>\n"
        with self._lock:
            self._write_raw(line)

    # ── Internals ─────────────────────────────────────────────────

    def _write_raw(self, s: str) -> None:
        if self._fp is None:
            # Pre-start (header) or post-stop write — open ad-hoc.
            with open(self.log_path, "a", encoding="utf-8") as fp:
                fp.write(s)
                fp.flush()
                try:
                    os.fsync(fp.fileno())
                except OSError:
                    pass
            return
        self._fp.write(s)
        self._fp.flush()
        try:
            os.fsync(self._fp.fileno())
        except OSError:
            pass
