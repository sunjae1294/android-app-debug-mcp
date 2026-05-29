"""Single-session state for the android-app-debug MCP server.

One server process holds at most one active ``Session`` at a time
(``_current``). ``start_debug_session`` populates it, every other tool reads
it via ``current()``, and ``finish_test`` / ``abort_session`` clear it.

State is process-local; a server crash drops the active session. Crash
recovery is out of scope because losing the server process also abandons
the emulator side of the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from android_ops import Adb
from logcat_writer import LogcatWriter
from marvis import MarvisParser


@dataclass
class TaskMeta:
    """The structured test task the agent is verifying."""

    feature: str
    ui_path: List[str] = field(default_factory=list)
    expected_behavior: str = ""
    report_format: str = "Pass/fail with one-paragraph justification."
    max_steps: int = 20
    extra_logcat_filters: List[str] = field(default_factory=list)


@dataclass
class Session:
    """All per-debug-session state. One per active emulator/app run."""

    package: str
    project_root: Optional[Path]
    run_dir: Path
    adb: Adb
    logcat: LogcatWriter
    task: TaskMeta
    parser: MarvisParser

    dev_w: int
    dev_h: int
    img_w: int
    img_h: int

    step: int = 0
    last_error: Optional[str] = None
    finished: bool = False

    # Accumulated per-step trace rows (mirrors trace.jsonl on disk).
    trace_rows: List[Dict[str, Any]] = field(default_factory=list)


_current: Optional[Session] = None
_LOCK = Lock()


def set_current(sess: Session) -> None:
    """Install ``sess`` as the active session. Raises if one already exists."""
    global _current
    with _LOCK:
        if _current is not None and not _current.finished:
            raise RuntimeError(
                "a debug session is already active; call finish_test or "
                "abort_session before starting a new one"
            )
        _current = sess


def current() -> Session:
    """Return the active session or raise if none is running."""
    with _LOCK:
        sess = _current
    if sess is None:
        raise RuntimeError(
            "no active debug session; call start_debug_session first"
        )
    return sess


def clear() -> None:
    """Drop the active session reference."""
    global _current
    with _LOCK:
        _current = None
