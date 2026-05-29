---
name: android-app-debug-mcp
description: End-to-end Android debug loop on an emulator. Builds the APK with a Gradle task, boots a named AVD, installs and launches the target package, then drives a single feature via adb-injected UI actions while capturing a unified logcat-and-actions timeline plus a developer-facing test report. Use when the user wants to debug, test, verify, or reproduce a feature or bug of an Android app by driving an emulator and reflecting on the UI.
---

# android-app-debug-mcp

Tools live in the `android-app-debug` MCP server. Each tool's schema (parameters, defaults, descriptions) is authoritative — this document covers only the cross-cutting workflow, reasoning rules, and conventions that don't fit in a single tool description.

## When to use

- "Debug the login flow on the emulator and tell me where it breaks."
- "Verify that the new VoiceChat screen renders correctly after I changed `MainActivity.kt`."
- "Reproduce the FCM-token-staleness logcat error and show me the exact UI step that triggers it."
- "Test the wake-word fallback path on a fresh install."
- Any time the alternative would be juggling `./gradlew`, `adb install`, `adb shell am start`, `adb logcat`, and manual UI taps to validate a single feature.

One structured task per invocation. Compose a task, start a session, loop through observations and actions, call `finish_test`. Surface the returned report to the user. Repeat for the next task.

## Tools at a glance

- **Lifecycle**: `precheck`, `start_debug_session`, `finish_test`, `abort_session`.
- **Observation**: `observe_screen`.
- **Actions** (each writes a logcat marker, runs adb, settles, returns the new screen): `tap`, `long_press`, `type_text`, `scroll_dir`, `scroll_to_text_tool`, `press_home`, `press_back`, `wait`.

At most one debug session is active per server process. `start_debug_session` installs it; the action and observation tools operate on the current session implicitly. `finish_test` and `abort_session` clear it. There is no `session_id` parameter.

Every action and observation tool returns one text block (task metadata, step counter, last error, UI element list) and two image blocks (raw screenshot, annotated screenshot).

## Workflow

1. **Pre-flight.** Call `precheck` and inspect the JSON. Resolve:
   - **AVD** — if `default_avd` is null OR `available_avds` has more than one entry and there's no other reason to prefer one, ask the user.
   - **Device serial** — if `connected_devices` contains more than one emulator (or a mix of real device + emulator and the user hasn't specified), ask. Real-device serials look like `R5CY50ANT9A`; emulator serials always start with `emulator-`.
   - **Project root / package** — if not stated in the user's request, ask.

   If `adb.found` or `emulator.found` is `false`, stop and tell the user to install the Android SDK / set `ANDROID_SDK_ROOT`.

2. **Start the session.** Call `start_debug_session` with the resolved values. The first observation comes back with the raw + annotated screenshots — start reasoning from there.

3. **Loop: observe → decide → act.** Look at the raw screenshot first to establish current state. Reconcile with the prior turn — if the previous action's expected outcome didn't happen, acknowledge it and adapt. Then call exactly one action tool. The response is the next observation; repeat until a verdict is reachable.

   Hard cap of **20 steps**. Once the step counter hits `max_steps`, action tools refuse — call `finish_test` next.

4. **Finish.** Call `finish_test(outcome="pass"|"fail"|"inconclusive", report=...)`. Compose `report` as developer-facing markdown — include UX observations as bullets and embed screenshots inline (`![alt](screenshots/step03_raw.jpg)`). Captured frames live at `screenshots/stepNN_raw.jpg` and `screenshots/stepNN_annotated.jpg` relative to the run directory. The tool returns the full report markdown as a string — surface it directly to the user.

## Information trust hierarchy

1. **Raw screenshot (GROUND TRUTH)** — the unmodified screenshot is the authoritative source for what is on screen.
2. **Annotated screenshot + UI element list (SUPPLEMENTARY)** — useful for locating coordinates of interactive elements, but may include phantom elements (occluded by the keyboard, behind overlays, off-screen, or invisible accessibility nodes). Always cross-reference against the raw screenshot.
3. **Conversation history (INTENDED ACTIONS ONLY)** — records what was tried, not what actually happened. Actions can fail silently. Verify outcomes against the **current** raw screenshot, not against history.

## Reasoning order at every step

1. Look at the raw screenshot first and determine the actual current state.
2. Reconcile with prior turns: if the previous action's expected outcome did not happen, acknowledge it and adapt — do not assume success.
3. Use the annotated screenshot + UI element list to find coordinates for the next action — only after confirming the target element is actually visible in the raw screenshot.
4. Pick exactly one action tool. Pass a past-tense `summary` (≤ 80 chars). Pass `ux_feedback` only when friction was observed on the current screen.

## Coordinate and action conventions

- All coordinates are in **image space**. The image is the device screenshot scaled down by 2×. Each observation reports the exact `img_w × img_h` to stay within; coordinates must satisfy `0 <= x < img_w` and `0 <= y < img_h`.
- Coordinates can come from the UI element list (use `center` or any point inside `bounds`) OR from visual estimation when the target is visible in the raw screenshot but missing from the list. The list is not exhaustive.
- Use `type_text` rather than tapping individual keyboard keys. If a field already contains placeholder or default text, clear it first.
- `scroll_dir`'s `direction` is opposite to a finger swipe — to reveal content lower on the page, use `direction="down"`.
- Use `scroll_to_text_tool` when looking for an item by text in a long list — much faster than repeated scroll-and-look.
- Tap auto-complete dropdown entries when they appear (those fields are usually enums).
- `summary` becomes the inline logcat marker for the step; write past tense ("Tapped the Send button.") since the timeline reader sees it after the action fired.
- `ux_feedback` is a one-line note about friction visible on the **current** screen; empty when nothing is notable. These accumulate as durable per-step evidence to fold into the final report.

## UI/UX observation tracking

You are also a usability tester. While driving the app, watch for friction a developer should know about, **even if the feature passes**. Capture each one as `ux_feedback` on the step where it was first observed, then fold the accumulated set into the `report` markdown passed to `finish_test` (as a `## UI/UX observations` section with screenshot links).

What counts (one bullet each, concise and concrete):

- **Occlusions** — a critical control hidden behind the soft keyboard, a dialog, an overlay, or off-screen.
- **Recovery loops** — clipboard panels, autocomplete dropdowns, suggestion bars, IME quirks that took multiple steps to dismiss.
- **Unclear or missing feedback** — action fired but the screen gave no visible confirmation; error label flashed and disappeared; button stayed enabled where disabled-state would have been clearer.
- **Layout problems** — text fields too narrow, labels overlap, content cut off, important content below the fold without a scroll cue, touch targets too small.
- **Confusing labels or copy** — a button labeled "OK" where "Send" would be clearer; an error message that doesn't say what the user did wrong.
- **State-handling surprises** — typed text persists across re-focus when it shouldn't; counters or list state don't refresh; back navigation lands on the wrong screen.
- **Performance hitches** — visible jank, animation stalls, multi-second waits without a spinner.

Do NOT log: things obviously working as intended, generic praise, or things that couldn't actually be observed. Phrase each so a developer can act on it. If no UX issues were encountered, omit the section.

## `finish_test` report template

`report` is free-form markdown. Source UX bullets from the accumulated per-step `ux_feedback` entries — combine duplicates and tighten wording, but don't drop one recorded earlier or invent ones that weren't. Embed screenshot links with relative paths.

```markdown
The login screen displayed the inline error "Password required" within ~1.2s
of tapping Sign In with the password field empty.

## UI/UX observations

- Sign In button sits just above the keyboard with no padding; easy to mis-tap.

  ![Step 2 raw](screenshots/step02_raw.jpg)

## Verdict screen

![Step 4 raw](screenshots/step04_raw.jpg)
```

## Run artifacts

`start_debug_session` writes one directory per invocation under `<project_root>/android-app-debug/<YYYY-MM-DDTHH-MM-SS>/` by default (override with `ANDROID_APP_DEBUG_RUNS_DIR`). Each run directory contains:

- `logcat.log` — annotated logcat stream with inline `<<< MARVIS_DEV ... >>>` markers, one per fired action.
- `trace.jsonl` — one row per step with the action JSON.
- `screenshots/step{NN}_raw.jpg` and `step{NN}_annotated.jpg`.
- `report.md` — the verdict supplied to `finish_test` (also returned as the tool's response string).
- `timeline.md` — assembled report + logcat in one file. On disk for the user to browse later.
- `build_error.log` — only present if Gradle failed.

Add `android-app-debug/` to the Android project's `.gitignore`.

## Configuration

`~/.config/android-app-debug/config.json` holds non-secret defaults:

```json
{
  "android_sdk_path": "~/Library/Android/sdk",
  "default_avd_name": "Pixel_8_API_34"
}
```

Run `python3 <skill-dir>/scripts/onboard.py` once interactively to write it.

## Prerequisites

- Android SDK installed (`adb` and `emulator` reachable; default lookup at `~/Library/Android/sdk`).
- At least one AVD created via Android Studio's AVD Manager.
- Python 3.10+ with `mcp` and `pillow` installed: `pip install mcp pillow`.

## Emulator window mode

Default is a visible (windowed) emulator so the user can watch the run, catch issues that don't appear in logcat (visual artifacts, animations, focus changes), and intervene if anything goes wrong. The server never auto-starts emulators in headless mode.

For headless (CI), start the AVD beforehand with `-no-window` and pass `skip_install` / `skip_launch` as appropriate to `start_debug_session`.

## Troubleshooting

- **`no AVDs are installed on this machine`** — create one via Android Studio's *Tools* → *Device Manager* → *Create Device*, or via `avdmanager create avd -n MyEmu -k "system-images;android-34;google_apis;arm64-v8a"`. Then re-run `scripts/onboard.py` to set it as the default.
- **`multiple devices visible to adb`** — pass `serial="emulator-5554"` (or whatever) explicitly to `start_debug_session`. Lookup with `adb devices`.
- **`adb not found`** — install Android SDK platform-tools, or set `ANDROID_SDK_ROOT`.
- **`emulator did not finish booting`** — the AVD may need a snapshot reset; try `~/Library/Android/sdk/emulator/emulator -avd <name> -wipe-data` once manually, then re-run.
- **uiautomator dump fails** — some apps block accessibility services. Insert a `wait` step, or pass `apk_path` pointing to a debug build with `android:debuggable="true"`.
- **Coordinates miss their target** — image-space coordinates are 1/2 device resolution; the server scales them up. If a known coordinate consistently misses, the AVD's display density may differ from what the Marvis parser expects.
