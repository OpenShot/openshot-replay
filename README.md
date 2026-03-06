# OpenShot Replay Suite

## Record it once. Replay it anytime. Catch UI regressions before users do.

UI behavior can drift over time, and manual re-testing is slow and inconsistent. This suite gives you a repeatable way to capture real user flows, replay them quickly, and verify that OpenShot still behaves the same.

It helps you:
- Reproducing UI behavior quickly
- Catching regressions in repeatable user flows
- Comparing trace output against known-good baselines

1. `record.py` captures your input actions and writes `*.actions.json`.
2. OpenShot writes trace files (`events`, `updates`, `selections`) during recording.
3. `replay.py` replays a single action file against OpenShot.
4. `tests.py` discovers all cases, replays each one, and asserts actual traces match expected traces.

## Repository Layout

```text
suite/
  record.py        # Record UI actions to *.actions.json
  replay.py        # Replay one recorded action file
  tests.py         # Discover and run all replay test cases
  init_replay.py   # Create baseline .osp project via replay
  assertions.py    # Baseline/state assertion helpers
  cases/           # *.actions.json test cases
  artifacts/
    home/          # Isolated OpenShot HOME profile
    traces/        # Expected recording traces (*.jsonl)
    runs/          # Actual traces captured during suite runs
```

By default, scripts target `../openshot-qt`. Override with:
- `--openshot-root /path/to/openshot-qt`
- or `OPENSHOT_QT_ROOT=/path/to/openshot-qt`

## Requirements

- Ubuntu + X11 session
- `xdotool`
- `wmctrl`
- `curl`
- `unzip`
- Python 3.10+
- `pynput` (recording + emergency Esc stop)

Install:

```bash
sudo apt update
sudo apt install -y xdotool wmctrl curl unzip python3 python3-pip
python3 -m pip install --user pynput
```

## Test Media Setup

Recordings in this repo expect test media at:

`suite/artifacts/home/openshot-testing`

Download and extract it:

```bash
mkdir -p suite/artifacts/home
curl -L -o /tmp/openshot-testing.zip https://s3.amazonaws.com/releases.openshot.org/testing/openshot-testing.zip
rm -rf suite/artifacts/home/openshot-testing
unzip -q /tmp/openshot-testing.zip -d suite/artifacts/home
```

## Standard Procedure

1. Record one case per file.
2. Do not record clicking the window close button at the end of the case.
3. Let `replay.py` / `tests.py` launch and close OpenShot.

## Record New Case

Example case name: `trim_clip`

```bash
python3 suite/record.py \
  --output suite/cases/trim_clip.actions.json \
  --lang es_ES.UTF-8 \
  --env QT_SCALE_FACTOR=1 \
  --openshot-arg=--web-backend=webengine \
  --openshot-root ../openshot-qt
```

Press `Esc` to stop recording.

This writes:
- `suite/cases/trim_clip.actions.json`
- `suite/artifacts/traces/trim_clip.events.jsonl`
- `suite/artifacts/traces/trim_clip.updates.jsonl`
- `suite/artifacts/traces/trim_clip.selections.jsonl`

Notes:
- By default, updates with `ignore_history=True` are excluded.
- Use `--include-ignored-updates` if you want all update noise captured.
- Cache-progress trace events are opt-in (`OPENSHOT_UI_TRACE_CACHE=1`) to avoid timing noise.
- Mouse wheel scroll is recorded/replayed (including modifier combos like `Ctrl+Scroll`).
- `--env KEY=VALUE` is repeatable for launch-time env overrides.
- `--lang` sets both `LANG` and `LC_ALL`.
- `--openshot-arg ARG` is repeatable for launch-time OpenShot args.
- Env passed via `--env/--lang` is saved into `meta.env` in the actions file.
- OpenShot args passed via `--openshot-arg` are saved into `meta.openshot_args`.

## Replay One Case

```bash
python3 suite/replay.py \
  --actions suite/cases/trim_clip.actions.json \
  --openshot-root ../openshot-qt
```

Useful options:
- `--speed 2.0` (faster replay)
- `--pointer-margin 56` (more tolerant window-edge clicks)
- `--debug` (verbose replay logging)
- `--env KEY=VALUE` / `--lang ...` (override recorded launch env)
- `--openshot-arg ARG` (append/override recorded OpenShot launch args)

Emergency stop: press physical `Esc`.

Replay automatically applies `meta.env` from the actions file when launching OpenShot.
Replay also automatically applies `meta.openshot_args` from the actions file.

Backend-specific examples:

```bash
# Record for webengine
python3 suite/record.py --output suite/cases/preview_webengine.actions.json --openshot-arg=--web-backend=webengine

# Record for qwidget
python3 suite/record.py --output suite/cases/preview_qwidget.actions.json --openshot-arg=--web-backend=qwidget
```

## Run All Cases

```bash
python3 suite/tests.py --cases suite/cases --openshot-root ../openshot-qt
```

Run one or more specific cases:

```bash
python3 suite/tests.py --cases suite/cases --case clips_qwidget
python3 suite/tests.py --cases suite/cases --case clips_qwidget.actions.json --case files_qwidget
```

Faster run:

```bash
python3 suite/tests.py --cases suite/cases --speed 2.0
```

Float-tolerant assertions (useful for minor timing/pixel drift):

```bash
python3 suite/tests.py --cases suite/cases --float-tol 0.10
```

Locale/env across all cases:

```bash
python3 suite/tests.py --cases suite/cases --lang es_ES.UTF-8 --env QT_SCALE_FACTOR=1
```

OpenShot args across all cases:

```bash
python3 suite/tests.py --cases suite/cases --openshot-arg=--web-backend=webengine
```

`tests.py` behavior:
- Launches OpenShot per case
- Replays case actions
- Captures actual traces
- Strictly compares expected vs actual unified `events` trace (if present)
- Also compares expected vs actual `updates` / `selections` traces (unless disabled per case)
- Prints end summary

Unified `events` currently includes:
- `update`
- `selection`
- `action_triggered`
- `dialog_lifecycle`
- `dock_visibility`
- `cache_progress` (only when `OPENSHOT_UI_TRACE_CACHE=1`)
