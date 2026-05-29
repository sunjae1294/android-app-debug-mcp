<div align="center">

# android-app-debug-mcp

**Drive an Android emulator from your LLM. Build, install, launch, and test a feature end-to-end — get a unified logcat-and-actions timeline back.**

[![MCP](https://img.shields.io/badge/protocol-MCP-blue)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-green.svg)](https://www.python.org/downloads/)

</div>

---

`android-app-debug-mcp` is a Model Context Protocol server that turns one structured test task ("verify the login screen rejects an empty password") into one auditable debug run — APK built, AVD booted, app launched, UI driven through adb, logcat captured, screenshots saved, verdict written.

Every UI action lands as a typed MCP tool call, so the entire decision trail is visible in your LLM client's chat history and reproducible from `trace.jsonl` and `logcat.log` on disk.

## Highlights

- **Build → boot → launch → drive → verdict, in one session.** Gradle, AVD, install, launch, logcat, and the Marvis UI-tree parser are all orchestrated behind a single `start_debug_session` call.
- **Unified timeline.** Every action writes an inline `<<< MARVIS_DEV ... >>>` marker into the live logcat stream, so cause and effect sit next to each other in `timeline.md`.
- **Auditable by default.** Every captured frame, every action, and every UX observation lands under `<project_root>/android-app-debug/<timestamp>/`.

## Install

### As a Claude Code plugin (recommended)

```bash
# Inside Claude Code:
/plugin marketplace add /path/to/android-app-debug-mcp
/plugin install android-app-debug-mcp@FCLab.SKKU
```

The bundled plugin manifest registers the MCP server automatically via `${CLAUDE_PLUGIN_ROOT}/scripts/server.py`.

### As a standalone MCP server

```bash
claude mcp add marvis-dev -- uv run --project /absolute/path/to/android-app-debug-mcp python /absolute/path/to/android-app-debug-mcp/scripts/server.py
```

Or copy `.mcp.json.example` to `.mcp.json` in your project root and edit the path.

### Prerequisites

- [`uv`](https://docs.astral.sh/uv/) on `PATH` — manages Python and dependencies automatically (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`).
- Android SDK with `adb` and `emulator` on `PATH` (default lookup `~/Library/Android/sdk`).
- At least one AVD created via Android Studio's AVD Manager.

`uv` reads `pyproject.toml` and installs `mcp` + `pillow` into an isolated environment on first launch — no manual `pip install` step, no global Python pollution.

### One-time setup (optional)

```bash
python3 scripts/onboard.py
```

Writes `~/.config/android-app-debug/config.json` with non-secret defaults (SDK path, default AVD).

## Tool reference

| Tool | Purpose |
| --- | --- |
| `precheck` | JSON readiness report — AVDs, devices, SDK paths |
| `start_debug_session` | Build, boot, install, launch; returns first observation |
| `observe_screen` | Re-capture the current screen without firing input |
| `tap`, `long_press`, `type_text` | Touch and keyboard input |
| `scroll_dir`, `scroll_to_text_tool` | Scrolling |
| `press_home`, `press_back` | Hardware-key navigation |
| `wait` | Sleep, then re-observe |
| `finish_test` | Write `report.md` + `timeline.md`, stop logcat, return the report markdown |
| `abort_session` | Stop logcat without writing a verdict report |

Each action tool returns one text block (task metadata, step counter, UI element list) and two image blocks (raw screenshot, annotated screenshot). Coordinates are in image space (half device resolution); the server scales them up before invoking `adb input`.

See [`SKILL.md`](./SKILL.md) for the full agent-facing contract: workflow, information-trust hierarchy, coordinate conventions, UX-observation rubric, and the `report` markdown template.

## Run artifacts

Each `start_debug_session` invocation writes one directory under `<project_root>/android-app-debug/<YYYY-MM-DDTHH-MM-SS>/` (override with `$ANDROID_APP_DEBUG_RUNS_DIR`):

```
android-app-debug/2026-05-28T22-15-04/
├── logcat.log              # annotated logcat with inline <<< MARVIS_DEV ... >>> markers
├── trace.jsonl             # one row per step with the action JSON
├── screenshots/
│   ├── step01_raw.jpg
│   ├── step01_annotated.jpg
│   └── ...
├── report.md               # verdict supplied to finish_test
├── timeline.md             # report + logcat in one file — the file to read after a run
└── build_error.log         # only if Gradle failed
```

Add `android-app-debug/` to your project's `.gitignore`.


## Credits

Built by [Team Marvis](https://marvis-ai.com/) at SungKyunKwan University.
## License

MIT — see [LICENSE](./LICENSE).
