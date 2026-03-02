#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import string
import threading
import time
from pathlib import Path

from replay import (
    close_app,
    focus_window,
    get_window_pid,
    launch_openshot,
    maximize_window,
    parse_env_assignments,
    xdotool,
    wait_for_window,
)


def try_import_pynput():
    try:
        from pynput import keyboard, mouse
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing dependency 'pynput'. Install with:\n"
            "  python3 -m pip install --user pynput"
        ) from exc
    return keyboard, mouse


def normalize_key(key):
    # Prefer pynput KeyCode.char for printable keys; this is more reliable
    # than str(key) when Shift/layout state is involved.
    key_char = getattr(key, "char", None)
    if isinstance(key_char, str) and key_char:
        if len(key_char) == 1 and key_char in string.printable and key_char not in ("\x0b", "\x0c", "\r", "\n", "\t"):
            return key_char

    # Fallback to virtual-key mapping for stable US keyboard capture.
    vk = getattr(key, "vk", None)
    if isinstance(vk, int):
        if 65 <= vk <= 90:   # A-Z keys
            return chr(vk).lower()
        if 48 <= vk <= 57:   # 0-9 keys
            return chr(vk)

    key_str = str(key)
    special = {
        "Key.enter": "Return",
        "Key.esc": "Escape",
        "Key.space": "space",
        "Key.backspace": "BackSpace",
        "Key.delete": "Delete",
        "Key.tab": "Tab",
        "Key.left": "Left",
        "Key.right": "Right",
        "Key.up": "Up",
        "Key.down": "Down",
        "Key.home": "Home",
        "Key.end": "End",
        "Key.page_up": "Page_Up",
        "Key.page_down": "Page_Down",
        "Key.insert": "Insert",
        "Key.shift": "Shift_L",
        "Key.shift_r": "Shift_R",
        "Key.ctrl": "Control_L",
        "Key.ctrl_r": "Control_R",
        "Key.alt": "Alt_L",
        "Key.alt_r": "Alt_R",
        "Key.cmd": "Super_L",
        "Key.cmd_r": "Super_R",
    }
    if key_str in special:
        return special[key_str]

    if len(key_str) == 3 and key_str.startswith("'") and key_str.endswith("'"):
        return key_str[1:-1]
    # Ignore unknown opaque key tokens such as "<16842487>".
    if key_str.startswith("<") and key_str.endswith(">"):
        return None
    return key_str if key_str in string.printable else None


MODIFIER_KEYS = {
    "Shift_L",
    "Shift_R",
    "Control_L",
    "Control_R",
    "Alt_L",
    "Alt_R",
    "Super_L",
    "Super_R",
}


class Recorder:
    def __init__(self, output, move_min_interval=0.05, move_min_delta=8):
        self.output = Path(output)
        self.move_min_interval = float(move_min_interval)
        self.move_min_delta = int(move_min_delta)
        self.start = time.monotonic()
        self.last_event_ts = self.start
        self.last_move_ts = self.start
        self.last_move_xy = None
        self.actions = []
        self.stop = False
        self._pressed_modifiers = set()

    def _insert_sleep_since_last(self, now):
        delta = now - self.last_event_ts
        if delta > 0:
            self.actions.append({"action": "sleep", "seconds": round(delta, 4)})
        self.last_event_ts = now

    def _record_move(self, x, y):
        now = time.monotonic()
        if self.last_move_xy is not None:
            dx = abs(x - self.last_move_xy[0])
            dy = abs(y - self.last_move_xy[1])
            if now - self.last_move_ts < self.move_min_interval and dx < self.move_min_delta and dy < self.move_min_delta:
                return
        self._insert_sleep_since_last(now)
        self.actions.append({"action": "move", "x": int(x), "y": int(y)})
        self.last_move_xy = (int(x), int(y))
        self.last_move_ts = now

    def on_move(self, x, y):
        if self.stop:
            return False
        self._record_move(x, y)

    def on_click(self, x, y, button, pressed):
        if self.stop:
            return False
        self._record_move(x, y)
        name = str(button)
        b = 1
        if name.endswith("right"):
            b = 3
        elif name.endswith("middle"):
            b = 2
        self.actions.append({"action": "mousedown" if pressed else "mouseup", "button": b})

    def on_scroll(self, x, y, dx, dy):
        if self.stop:
            return False

        now = time.monotonic()
        self._insert_sleep_since_last(now)
        self.last_move_xy = (int(x), int(y))
        self.last_move_ts = now

        def quantize(axis):
            try:
                value = float(axis)
            except Exception:
                return 0
            if value == 0:
                return 0
            steps = int(round(abs(value)))
            if steps < 1:
                steps = 1
            return steps if value > 0 else -steps

        qdx = quantize(dx)
        qdy = quantize(dy)
        if qdx == 0 and qdy == 0:
            return
        self.actions.append({"action": "scroll", "dx": qdx, "dy": qdy})

    def on_press(self, key):
        if self.stop:
            return False
        now = time.monotonic()
        key_name = None

        # Prefer physical A-Z key mapping using vk + Shift state, to avoid
        # layout/IME surprises (e.g. Arabic chars captured for Shift+letter).
        vk = getattr(key, "vk", None)
        if isinstance(vk, int) and 65 <= vk <= 90:
            base = chr(vk)
            shift_held = any(k in self._pressed_modifiers for k in ("Shift_L", "Shift_R"))
            key_name = base if shift_held else base.lower()
        else:
            key_name = normalize_key(key)
        if not key_name:
            return
        if key_name == "Escape":
            self._insert_sleep_since_last(now)
            self.stop = True
            return False
        self._insert_sleep_since_last(now)
        if key_name in MODIFIER_KEYS:
            if key_name not in self._pressed_modifiers:
                self.actions.append({"action": "keydown", "key": key_name})
                self._pressed_modifiers.add(key_name)
        else:
            self.actions.append({"action": "key", "key": key_name})

    def on_release(self, key):
        if self.stop:
            return False
        key_name = normalize_key(key)
        if not key_name:
            return
        if key_name in MODIFIER_KEYS and key_name in self._pressed_modifiers:
            now = time.monotonic()
            self._insert_sleep_since_last(now)
            self.actions.append({"action": "keyup", "key": key_name})
            self._pressed_modifiers.remove(key_name)
        return

    def write(self, meta=None):
        # Ensure no modifier remains logically held at end of recording.
        if self._pressed_modifiers:
            now = time.monotonic()
            self._insert_sleep_since_last(now)
            for key_name in sorted(self._pressed_modifiers):
                self.actions.append({"action": "keyup", "key": key_name})
            self._pressed_modifiers.clear()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"actions": self.actions}
        if meta:
            payload["meta"] = meta
        self.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Record UI actions to JSON (stop with Esc)")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--move-min-interval", type=float, default=0.05)
    parser.add_argument("--move-min-delta", type=int, default=8)
    parser.add_argument(
        "--window-name",
        default="OpenShot Video Editor",
        help="Window title regex/name used to locate the main window",
    )
    parser.add_argument(
        "--home",
        default=str((Path(__file__).resolve().parent / "artifacts" / "home").resolve()),
        help="HOME directory for OpenShot profile when launching",
    )
    parser.add_argument(
        "--openshot-root",
        default=os.getenv("OPENSHOT_QT_ROOT", ""),
        help="Path to openshot-qt repository (or set OPENSHOT_QT_ROOT).",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Attach to already running OpenShot window instead of launching",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Do not close launched OpenShot when recording stops",
    )
    parser.add_argument(
        "--trace-dir",
        default=str((Path(__file__).resolve().parent / "artifacts" / "traces").resolve()),
        help="Directory to write in-app JSONL traces during recording",
    )
    parser.add_argument(
        "--disable-trace",
        action="store_true",
        help="Disable in-app JSONL trace recording",
    )
    parser.add_argument(
        "--include-ignored-updates",
        action="store_true",
        help="Include updates emitted while UpdateManager.ignore_history=True",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra environment variable for launched OpenShot (repeatable)",
    )
    parser.add_argument(
        "--lang",
        default="",
        help="Convenience locale value. Sets both LANG and LC_ALL.",
    )
    args = parser.parse_args()

    keyboard, mouse = try_import_pynput()
    rec = Recorder(args.output, args.move_min_interval, args.move_min_delta)

    home_dir = Path(args.home).resolve()
    home_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = Path(args.trace_dir).resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)

    output_base = Path(args.output).resolve().name
    if output_base.endswith(".actions.json"):
        output_base = output_base[: -len(".actions.json")]
    else:
        output_base = Path(args.output).resolve().stem
    updates_trace = trace_dir / f"{output_base}.updates.jsonl"
    selections_trace = trace_dir / f"{output_base}.selections.jsonl"
    events_trace = trace_dir / f"{output_base}.events.jsonl"
    trace_env = {}
    if not args.disable_trace:
        trace_env = {
            "OPENSHOT_UI_TRACE": "1",
            "OPENSHOT_UI_TRACE_UPDATES": str(updates_trace),
            "OPENSHOT_UI_TRACE_SELECTIONS": str(selections_trace),
            "OPENSHOT_UI_TRACE_EVENTS": str(events_trace),
            "OPENSHOT_UI_TRACE_INCLUDE_LOAD": "0",
            "OPENSHOT_UI_TRACE_INCLUDE_IGNORED": "1" if args.include_ignored_updates else "0",
        }
        for p in (updates_trace, selections_trace, events_trace):
            if p.exists():
                p.unlink()
    try:
        cli_env = parse_env_assignments(args.env)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.lang:
        cli_env["LANG"] = args.lang
        cli_env["LC_ALL"] = args.lang
    launch_env = {}
    launch_env.update(trace_env)
    launch_env.update(cli_env)

    proc = None
    try:
        if not args.no_launch:
            proc = launch_openshot(
                home_dir,
                extra_env=launch_env or None,
                openshot_root=args.openshot_root or None,
            )

        window_id = wait_for_window(args.window_name, timeout=40.0)
        focus_window(window_id)
        maximize_window(window_id)
        focus_window(window_id)

        window_pid = get_window_pid(window_id)
        if window_pid is None:
            raise SystemExit("Could not determine OpenShot window PID")
        if proc and proc.pid != window_pid:
            raise SystemExit(
                f"Window PID {window_pid} does not match launched OpenShot PID {proc.pid}"
            )
    except Exception:
        close_app(proc)
        raise

    print(
        "Recording started on window id",
        window_id,
        "(PID",
        window_pid,
        "). Press Esc to stop.",
    )
    mouse_listener = mouse.Listener(
        on_move=rec.on_move,
        on_click=rec.on_click,
        on_scroll=rec.on_scroll,
    )
    key_listener = keyboard.Listener(
        on_press=rec.on_press,
        on_release=rec.on_release,
    )

    stop_notice = {"msg": None}

    def window_exists(wid):
        probe = xdotool("getwindowname", str(wid), check=False, capture=True)
        return probe.returncode == 0

    def monitor_shutdown():
        while not rec.stop:
            if proc and proc.poll() is not None:
                stop_notice["msg"] = "OpenShot process exited; stopping recording."
                rec.stop = True
                break
            if not window_exists(window_id):
                stop_notice["msg"] = "OpenShot main window closed; stopping recording."
                rec.stop = True
                break
            time.sleep(0.2)

        if rec.stop:
            key_listener.stop()
            mouse_listener.stop()

    monitor_thread = threading.Thread(target=monitor_shutdown, daemon=True)

    mouse_listener.start()
    key_listener.start()
    monitor_thread.start()
    key_listener.join()
    mouse_listener.stop()
    monitor_thread.join(timeout=0.5)
    meta = {"env": cli_env} if cli_env else None
    rec.write(meta=meta)
    print(f"Wrote {len(rec.actions)} actions to {shlex.quote(str(rec.output))}")
    if not args.disable_trace:
        if args.no_launch:
            print("Trace requested but --no-launch was used; ensure OpenShot was launched with OPENSHOT_UI_TRACE env vars.")
        else:
            print(f"Wrote update trace to {shlex.quote(str(updates_trace))}")
            print(f"Wrote selection trace to {shlex.quote(str(selections_trace))}")
            print(f"Wrote unified events trace to {shlex.quote(str(events_trace))}")
    if stop_notice["msg"]:
        print(stop_notice["msg"])

    if proc and not args.keep_open:
        close_app(proc)


if __name__ == "__main__":
    main()
