"""Capture-and-render helper shared by ``observe_screen`` and every action tool.

The pipeline:

1. Screencap the device (PNG via ``adb exec-out screencap -p``).
2. Resize to ``img_w x img_h`` (half device resolution) so Marvis bounding boxes
   land correctly and so we stay under provider-side per-image dimension caps.
3. Dump the uiautomator XML and parse it into a numbered element list.
4. Annotate the resized screenshot with bounding boxes + labels.
5. Persist both screenshots (JPEG q=80) to ``<run_dir>/screenshots/`` for
   post-run inspection.
6. Return an MCP-friendly payload: two ImageContent blocks (raw + annotated)
   plus one TextContent block carrying the UI element list and run metadata
   (step counter, screen dims, last error from the previous step).

Each call advances ``session.step`` by one and appends a row to
``trace.jsonl``; pure observations count as steps so each capture appears
in the timeline with its own screenshot.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp.types import ImageContent, TextContent
from PIL import Image as _Image

from session import Session


JPEG_QUALITY = 80


def _resize_png_b64(b64: str, target_w: int, target_h: int) -> str:
    """Resize a base64-encoded PNG to ``(target_w, target_h)`` and re-encode."""
    raw = base64.b64decode(b64)
    img = _Image.open(io.BytesIO(raw))
    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), _Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def _png_b64_to_jpeg_b64(png_b64: str, quality: int = JPEG_QUALITY) -> str:
    """Re-encode a base64 PNG as base64 JPEG. Flattens alpha onto white."""
    img = _Image.open(io.BytesIO(base64.b64decode(png_b64)))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        flat = _Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = flat
    elif img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(out.getvalue()).decode("ascii")


def _save_bytes_b64(b64: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(b64))


def capture_and_render(
    sess: Session,
    *,
    action_meta: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> List[Any]:
    """Take a snapshot of the device and return the MCP content list.

    ``action_meta`` is the action JSON that just fired (None for pure
    observations). ``note`` is an optional error string to include in the
    text payload (e.g. an action execution failure).
    """
    # --- 1. Screencap + UI dump ---
    raw_b64 = ""
    try:
        raw_full = sess.adb.screencap_b64()
        raw_b64 = _resize_png_b64(raw_full, sess.img_w, sess.img_h)
    except Exception as e:
        sess.last_error = f"screencap failed: {e}"

    ui_xml = ""
    try:
        ui_xml = sess.adb.dump_ui_tree()
    except Exception as e:
        sess.last_error = (sess.last_error + " | " if sess.last_error else "") + f"uiautomator dump failed: {e}"

    # --- 2. Parse + annotate ---
    if ui_xml:
        ui_elements_str = sess.parser.parse(ui_xml, (sess.img_w, sess.img_h))
    else:
        ui_elements_str = "No UI elements available."
    annotated_b64 = sess.parser.annotate_base64(raw_b64) if raw_b64 else ""

    # --- 3. Re-encode for transport: JPEG ~80 cuts payload by ~3-5x vs PNG ---
    raw_jpeg_b64 = _png_b64_to_jpeg_b64(raw_b64) if raw_b64 else ""
    annotated_jpeg_b64 = _png_b64_to_jpeg_b64(annotated_b64) if annotated_b64 else ""

    # --- 4. Persist artifacts ---
    sess.step += 1
    step_n = sess.step
    shots_dir = sess.run_dir / "screenshots"
    if raw_jpeg_b64:
        _save_bytes_b64(raw_jpeg_b64, shots_dir / f"step{step_n:02d}_raw.jpg")
    if annotated_jpeg_b64:
        _save_bytes_b64(annotated_jpeg_b64, shots_dir / f"step{step_n:02d}_annotated.jpg")

    # --- 4. Trace row ---
    trace_row: Dict[str, Any] = {
        "step": step_n,
        "action": action_meta,
        "note": note,
    }
    sess.trace_rows.append(trace_row)
    trace_path = sess.run_dir / "trace.jsonl"
    with trace_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(trace_row) + "\n")

    # --- 5. Build the MCP content list ---
    pending_error = sess.last_error
    sess.last_error = None  # consumed by this observation

    summary_lines: List[str] = [
        f"# Step {step_n}/{sess.task.max_steps} — observation",
    ]
    if step_n == 1:
        summary_lines += [
            "",
            f"- **Feature under test**: {sess.task.feature}",
            f"- **UI path hint**: {', '.join(sess.task.ui_path) if sess.task.ui_path else '(unspecified)'}",
            f"- **Expected behavior**: {sess.task.expected_behavior or '(unspecified)'}",
            f"- **Report format requested**: {sess.task.report_format}",
            f"- **Target package**: {sess.package}",
            f"- **Image resolution** (coords must satisfy `0 <= x < {sess.img_w}` and `0 <= y < {sess.img_h}`): {sess.img_w} x {sess.img_h}",
        ]
    if note:
        summary_lines.append(f"- **Note from this step**: {note}")
    if pending_error:
        summary_lines.append(f"- **Last error**: {pending_error}")
    if step_n >= sess.task.max_steps:
        summary_lines.append(
            f"- **WARNING**: step counter hit max_steps={sess.task.max_steps}. "
            "Call `finish_test` next — no further action tools will be accepted."
        )

    summary_lines += [
        "",
        "## UI elements detected on screen",
        "(bounds in `[xmin,ymin][xmax,ymax]`, center coords supplied per row)",
        "",
        ui_elements_str,
    ]
    if step_n == 1:
        summary_lines += [
            "",
            "Raw screenshot is the first attached image (GROUND TRUTH).",
            "Annotated screenshot with bounding-box labels is the second attached image (SUPPLEMENTARY).",
        ]
    text_block = TextContent(type="text", text="\n".join(summary_lines))

    content: List[Any] = [text_block]
    if raw_jpeg_b64:
        content.append(ImageContent(type="image", data=raw_jpeg_b64, mimeType="image/jpeg"))
    if annotated_jpeg_b64:
        content.append(ImageContent(type="image", data=annotated_jpeg_b64, mimeType="image/jpeg"))
    return content
