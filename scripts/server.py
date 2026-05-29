"""MCP server for end-to-end Android debug sessions.

Tools:

- ``precheck`` — readiness JSON: AVDs available, devices attached, SDK paths.
- ``start_debug_session`` — build the APK, boot the AVD, install + launch
  the package, start a per-PID logcat stream, and return a ``session_id``
  plus the first screen observation (screenshot + parsed UI element list).
- ``observe_screen`` — re-capture the current screen for a live session.
- ``tap`` / ``long_press`` / ``type_text`` / ``scroll_dir`` /
  ``scroll_to_text_tool`` / ``press_home`` / ``press_back`` / ``wait`` —
  per-action tools. Each writes a ``<<< MARVIS_DEV ... >>>`` marker into
  the session's logcat file, executes the action via adb, settles, then
  returns a fresh observation. Marker, action, and resulting screenshot
  are written as one bundle so the timeline stays causally ordered.
- ``finish_test`` — write ``report.md`` + ``timeline.md`` under the run
  directory, stop logcat (capturing the crash buffer on the way out), and
  release the session.
- ``abort_session`` — stop logcat and release the session without writing
  a verdict report.

Coordinate convention: all ``(x, y)`` parameters are in *image* space,
where the image is the device screenshot scaled down by SCALE_FACTOR=2.
The server scales them back up to device pixels before calling
``adb input``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# scripts/ is on sys.path when the MCP server is launched as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

from android_ops import (
    Adb,
    CommandError,
    android_sdk_root,
    build_apk,
    scroll,
    scroll_to_text,
)
from emulator import (
    ensure_running,
    list_attached_devices,
    list_attached_emulators,
    list_avds,
)
from logcat_writer import LogcatWriter
from marvis import MarvisParser
from observe import capture_and_render
from paths import config_path, load_config, runs_root
from session import Session, TaskMeta, clear, current, set_current


# Hard upper bound on UI steps per debug session — defensive cap to keep a
# runaway client from chewing through emulator state forever.
MAX_STEPS_HARD_CAP = 20

mcp = FastMCP("marvis-dev")


# ── helpers ────────────────────────────────────────────────────────────

def _make_run_dir(project_root: Optional[Path]) -> Path:
    """Resolve the per-run output directory, mirroring debug_run.py's logic."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if os.environ.get("ANDROID_APP_DEBUG_RUNS_DIR", "").strip():
        base = runs_root()
    elif project_root is not None:
        base = project_root.resolve() / "android-app-debug"
    else:
        base = runs_root()
    run_dir = base / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _validate_project_root(project_root: Path) -> Path:
    project_root = Path(os.path.expanduser(str(project_root))).resolve()
    if not project_root.exists():
        raise RuntimeError(f"project_root does not exist: {project_root}")
    has_gradlew = (project_root / "gradlew").exists()
    has_settings = any(
        (project_root / name).exists()
        for name in ("settings.gradle", "settings.gradle.kts")
    )
    if not (has_gradlew or has_settings):
        raise RuntimeError(
            f"project_root {project_root} does not look like a Gradle project "
            "(no gradlew or settings.gradle{,.kts})"
        )
    return project_root


def _pick_serial(explicit: Optional[str], sdk_root: Path) -> str:
    """Pick an adb serial to drive when none was explicitly supplied."""
    if explicit:
        return explicit
    attached = list_attached_devices(sdk_root=sdk_root)
    if not attached:
        raise RuntimeError(
            "no devices visible to adb after emulator boot. Check `adb devices`."
        )
    if len(attached) == 1:
        return attached[0][0]
    emus = list_attached_emulators(sdk_root=sdk_root)
    if len(emus) == 1:
        return emus[0]
    listing = ", ".join(f"{s} ({k})" for s, k in attached)
    raise RuntimeError(
        f"multiple devices visible to adb ({listing}). Pass `serial` explicitly."
    )


def _assemble_timeline(run_dir: Path) -> Path:
    """Produce timeline.md = report.md + a fenced block of logcat.log."""
    report = (run_dir / "report.md").read_text(encoding="utf-8") if (run_dir / "report.md").exists() else "# (no report)\n"
    logcat = (run_dir / "logcat.log").read_text(encoding="utf-8", errors="replace") if (run_dir / "logcat.log").exists() else ""
    timeline = run_dir / "timeline.md"
    timeline.write_text(
        report
        + "\n\n## Timeline (logcat with inline marvis_dev markers)\n\n"
        + "```\n"
        + logcat
        + ("\n" if not logcat.endswith("\n") else "")
        + "```\n",
        encoding="utf-8",
    )
    return timeline


def _ensure_can_act(sess: Session) -> None:
    """Reject action tools once the session is exhausted or already finished."""
    if sess.finished:
        raise RuntimeError("session is already finished; start a new one")
    if sess.step >= sess.task.max_steps:
        raise RuntimeError(
            f"max_steps={sess.task.max_steps} reached; call finish_test next"
        )


def _to_dev_x(sess: Session, x: int) -> int:
    if sess.img_w <= 0:
        return int(x)
    return int(round(int(x) * sess.dev_w / sess.img_w))


def _to_dev_y(sess: Session, y: int) -> int:
    if sess.img_h <= 0:
        return int(y)
    return int(round(int(y) * sess.dev_h / sess.img_h))


def _record_marker_and_observe(
    sess: Session,
    *,
    action: Dict[str, Any],
    summary: str,
    ux_feedback: str,
    settle_seconds: float,
    extra_note: Optional[str] = None,
) -> List[Any]:
    """Write the logcat marker for this action, settle, then observe."""
    sess.logcat.write_marker(action, summary or "(no summary)", ux_feedback or "")
    time.sleep(max(0.0, settle_seconds))
    return capture_and_render(sess, action_meta=action, note=extra_note)


# ── tools ──────────────────────────────────────────────────────────────


@mcp.tool()
def precheck() -> str:
    """Return a JSON readiness report — AVDs, devices, SDK paths, config.

    Call this before ``start_debug_session`` to resolve which AVD and which
    adb serial to use without guessing.
    """
    config = load_config()
    cfg_path = config_path()
    sdk_override = config.get("android_sdk_path", "")
    sdk = android_sdk_root(sdk_override) if sdk_override else android_sdk_root()

    def _adb_status() -> Dict[str, Any]:
        try:
            from android_ops import adb_bin
            return {"found": True, "path": adb_bin(sdk)}
        except FileNotFoundError as e:
            return {"found": False, "error": str(e)}

    def _emu_status() -> Dict[str, Any]:
        try:
            from android_ops import emulator_bin
            return {"found": True, "path": emulator_bin(sdk)}
        except FileNotFoundError as e:
            return {"found": False, "error": str(e)}

    report = {
        "config": {"present": cfg_path.exists(), "path": str(cfg_path)},
        "android_sdk_path": sdk_override or None,
        "adb": _adb_status(),
        "emulator": _emu_status(),
        "available_avds": list_avds(sdk),
        "default_avd": config.get("default_avd_name") or None,
        "connected_devices": [
            {"serial": s, "type": kind, "state": "device"}
            for (s, kind) in list_attached_devices(sdk)
        ],
        "runs_dir": {
            "default_pattern": "<project-root>/android-app-debug/<ts>/",
            "env_override": os.environ.get("ANDROID_APP_DEBUG_RUNS_DIR") or None,
            "xdg_fallback": str(runs_root()),
        },
        "max_steps_hard_cap": MAX_STEPS_HARD_CAP,
    }
    return json.dumps(report, indent=2)


@mcp.tool()
def start_debug_session(
    project_root: str,
    package: str,
    feature: str,
    expected_behavior: str = "",
    ui_path: Optional[List[str]] = None,
    report_format: str = "Pass/fail with one-paragraph justification.",
    max_steps: int = MAX_STEPS_HARD_CAP,
    extra_logcat_filters: Optional[List[str]] = None,
    gradle_task: str = "assembleDebug",
    apk_path: Optional[str] = None,
    avd: Optional[str] = None,
    serial: Optional[str] = None,
    sdk: Optional[str] = None,
    skip_install: bool = False,
    skip_launch: bool = False,
) -> List[Any]:
    """Build, install, launch, and start a fresh debug session.

    Returns the first screen observation (text + raw + annotated images).
    Only one session may be active at a time; call ``finish_test`` or
    ``abort_session`` first if another session is already running.
    Hard caps ``max_steps`` at 20.
    """
    config = load_config()
    sdk_root = Path(os.path.expanduser(sdk or config.get("android_sdk_path") or str(android_sdk_root())))
    avd_name = avd or config.get("default_avd_name")

    project_root_p = _validate_project_root(Path(project_root))
    run_dir = _make_run_dir(project_root_p)

    # 1. Build (or accept --apk-path).
    if apk_path:
        apk_p = Path(os.path.expanduser(apk_path))
        if not apk_p.exists():
            raise RuntimeError(f"apk_path does not exist: {apk_p}")
    else:
        try:
            apk_p = build_apk(project_root_p, gradle_task)
        except CommandError as e:
            err_path = run_dir / "build_error.log"
            err_path.write_text(
                f"# gradle exit {e.code}\n\n## stdout\n\n{e.stdout}\n\n## stderr\n\n{e.stderr}\n",
                encoding="utf-8",
            )
            raise RuntimeError(
                f"gradle build failed (exit {e.code}); see {err_path}"
            ) from e

    # 2. Emulator.
    ensure_running(avd_name, sdk_root=sdk_root)
    chosen_serial = _pick_serial(serial, sdk_root)
    adb = Adb(serial=chosen_serial, sdk_root=sdk_root)

    # 3. Install + launch.
    if not skip_install:
        adb.install(apk_p)
    if not skip_launch:
        adb.force_stop(package)
        time.sleep(1.0)
        adb.launch(package)

    # 4. Logcat + session bookkeeping.
    dev_w, dev_h = adb.screen_size()
    img_w, img_h = dev_w // 2, dev_h // 2

    task = TaskMeta(
        feature=feature,
        ui_path=list(ui_path or []),
        expected_behavior=expected_behavior,
        report_format=report_format,
        max_steps=min(int(max_steps), MAX_STEPS_HARD_CAP),
        extra_logcat_filters=list(extra_logcat_filters or []),
    )

    logcat = LogcatWriter(
        adb=adb,
        package=package,
        log_path=run_dir / "logcat.log",
        extra_filters=task.extra_logcat_filters,
    )
    logcat.start()
    logcat.write_note(
        f"starting test '{task.feature}' max_steps={task.max_steps}"
    )

    sess = Session(
        package=package,
        project_root=project_root_p,
        run_dir=run_dir,
        adb=adb,
        logcat=logcat,
        task=task,
        parser=MarvisParser(),
        dev_w=dev_w,
        dev_h=dev_h,
        img_w=img_w,
        img_h=img_h,
    )
    set_current(sess)

    return capture_and_render(
        sess,
        action_meta={"name": "start_debug_session"},
        note=(
            f"run_dir={sess.run_dir} | "
            f"device_resolution={dev_w}x{dev_h} | apk={apk_p}"
        ),
    )


@mcp.tool()
def observe_screen() -> List[Any]:
    """Re-capture the current screen without firing any input action.

    Useful when the client wants to re-look (e.g. after a wait, or to confirm
    a state change happened) without firing input. Still counts against
    ``max_steps`` — every screenshot is a step in the timeline.
    """
    sess = current()
    if sess.finished:
        raise RuntimeError("session is finished")
    return capture_and_render(sess, action_meta={"name": "observe_screen"})


@mcp.tool()
def tap(
    x: int,
    y: int,
    summary: str,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Tap at image-space coordinates (x, y). Returns the new screen state.

    ``summary`` lands in the logcat marker for this step (past tense, single
    sentence). ``ux_feedback`` is an optional one-line UX observation about
    the current screen — it's appended to the marker so the timeline carries
    the friction note next to the action that surfaced it.
    """
    sess = current()
    _ensure_can_act(sess)
    action = {"name": "tap", "parameters": {"x": int(x), "y": int(y)}}
    sess.adb.tap(_to_dev_x(sess, x), _to_dev_y(sess, y))
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def long_press(
    x: int,
    y: int,
    summary: str,
    duration_ms: int = 800,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Long-press at image-space (x, y) for ``duration_ms`` milliseconds."""
    sess = current()
    _ensure_can_act(sess)
    action = {"name": "long_press", "parameters": {"x": int(x), "y": int(y), "duration_ms": int(duration_ms)}}
    sess.adb.long_press(_to_dev_x(sess, x), _to_dev_y(sess, y), int(duration_ms))
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def type_text(
    text: str,
    summary: str,
    x: Optional[int] = None,
    y: Optional[int] = None,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Type ``text`` into the currently focused field.

    If ``x`` and ``y`` are supplied, the field at those image-space coords is
    tapped first to grab focus.
    """
    sess = current()
    _ensure_can_act(sess)
    params: Dict[str, Any] = {"text": text}
    if x is not None:
        params["x"] = int(x)
    if y is not None:
        params["y"] = int(y)
    action = {"name": "type_text", "parameters": params}
    if x is not None and y is not None:
        sess.adb.tap(_to_dev_x(sess, x), _to_dev_y(sess, y))
        time.sleep(0.8)
    sess.adb.type_text(text)
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def scroll_dir(
    direction: str,
    summary: str,
    x: Optional[int] = None,
    y: Optional[int] = None,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Scroll the screen (or a localized region around image-space x,y).

    ``direction`` ∈ {"up","down","left","right"}. "down" reveals content
    below — direction is what the content does, not what the finger does.
    """
    sess = current()
    _ensure_can_act(sess)
    if direction not in {"up", "down", "left", "right"}:
        raise RuntimeError(f"invalid direction {direction!r}")
    params: Dict[str, Any] = {"direction": direction}
    if x is not None:
        params["x"] = int(x)
    if y is not None:
        params["y"] = int(y)
    action = {"name": "scroll", "parameters": params}
    if x is not None and y is not None:
        scroll(sess.adb, direction, (sess.dev_w, sess.dev_h), _to_dev_x(sess, x), _to_dev_y(sess, y))
    else:
        scroll(sess.adb, direction, (sess.dev_w, sess.dev_h))
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def scroll_to_text_tool(
    text: str,
    summary: str,
    direction: str = "down",
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Scroll repeatedly until ``text`` appears in the accessibility tree.

    Much more efficient than scrolling and re-observing in a loop when looking
    for a known label in a long list.
    """
    sess = current()
    _ensure_can_act(sess)
    if direction not in {"up", "down"}:
        raise RuntimeError(f"invalid direction {direction!r} (use up/down)")
    action = {"name": "scroll_to_text", "parameters": {"text": text, "direction": direction}}
    note: Optional[str] = None
    ok = scroll_to_text(sess.adb, text, (sess.dev_w, sess.dev_h), direction)
    if not ok:
        note = f"scroll_to_text could not find {text!r}"
        sess.last_error = note
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds, extra_note=note,
    )


@mcp.tool()
def press_home(
    summary: str,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Press the device HOME key (KeyEvent 3)."""
    sess = current()
    _ensure_can_act(sess)
    action = {"name": "home", "parameters": {}}
    sess.adb.keyevent(3)
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def press_back(
    summary: str,
    ux_feedback: str = "",
    settle_seconds: float = 1.0,
) -> List[Any]:
    """Press the device BACK key (KeyEvent 4)."""
    sess = current()
    _ensure_can_act(sess)
    action = {"name": "back", "parameters": {}}
    sess.adb.keyevent(4)
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=settle_seconds,
    )


@mcp.tool()
def wait(
    summary: str,
    duration_ms: int = 1000,
    ux_feedback: str = "",
) -> List[Any]:
    """Sleep for ``duration_ms`` then re-observe the screen.

    Use this when an animation, network call, or transition needs time to
    finish before the next decision.
    """
    sess = current()
    _ensure_can_act(sess)
    action = {"name": "wait", "parameters": {"duration_ms": int(duration_ms)}}
    time.sleep(max(0, int(duration_ms)) / 1000.0)
    return _record_marker_and_observe(
        sess, action=action, summary=summary, ux_feedback=ux_feedback,
        settle_seconds=0.0,
    )


@mcp.tool()
def finish_test(outcome: str, report: str) -> str:
    """End the debug session, write report.md + timeline.md, stop logcat.

    ``outcome`` is one of "pass" | "fail" | "inconclusive".
    ``report`` is a markdown string addressed to a developer. Include any
    UX observations and image links (``![alt](screenshots/step03_raw.jpg)``)
    inline — captured screenshots live under ``screenshots/stepNN_raw.jpg``
    and ``stepNN_annotated.jpg`` relative to the run directory.

    Returns the full ``report.md`` contents as a string, with the run
    directory path on the first line so the user can locate the artifacts.
    """
    sess = current()
    if outcome not in {"pass", "fail", "inconclusive"}:
        raise RuntimeError(f"invalid outcome {outcome!r} (use pass/fail/inconclusive)")

    sess.logcat.write_marker(
        {"name": "finish_test", "parameters": {"outcome": outcome}},
        f"Finished the test with outcome={outcome}.",
        "",
    )

    report_md = (
        f"# marvis_dev report\n\n"
        f"- **Feature**: {sess.task.feature}\n"
        f"- **Outcome**: `{outcome}`\n"
        f"- **Steps**: {sess.step}\n\n"
        f"---\n\n{report or '(no report supplied)'}\n"
    )

    report_path = sess.run_dir / "report.md"
    report_path.write_text(report_md, encoding="utf-8")

    try:
        sess.logcat.stop()
    except Exception:
        pass

    _assemble_timeline(sess.run_dir)
    sess.finished = True
    clear()
    return f"run_dir: {sess.run_dir}\n\n{report_md}"


@mcp.tool()
def abort_session(reason: str = "") -> str:
    """Stop logcat and free the active session without writing a verdict report.

    Use this when a session is unrecoverable mid-run (emulator crashed, app
    is wedged, user wants to bail out) — leaves whatever artifacts were
    already written on disk but does not produce ``report.md`` / ``timeline.md``.
    """
    try:
        sess = current()
    except RuntimeError:
        return "no active session"
    try:
        sess.logcat.write_note(f"session aborted: {reason or '(no reason)'}")
    except Exception:
        pass
    try:
        sess.logcat.stop()
    except Exception:
        pass
    sess.finished = True
    clear()
    return f"aborted session, run_dir={sess.run_dir}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
