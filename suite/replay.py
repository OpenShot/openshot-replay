#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import signal
import shutil
import subprocess
import threading
import time
import re
from pathlib import Path

DEBUG_REPLAY = False


REPLAY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OPENSHOT_ROOT = (REPLAY_ROOT.parent / "openshot-qt").resolve()


def run_cmd(args, check=True, capture=False):
    if capture:
        return subprocess.run(args, check=check, text=True, capture_output=True)
    return subprocess.run(args, check=check)


def xdotool(*parts, check=True, capture=False):
    cmd = ["xdotool", *parts]
    return run_cmd(cmd, check=check, capture=capture)


def wmctrl(*parts, check=True, capture=False):
    cmd = ["wmctrl", *parts]
    return run_cmd(cmd, check=check, capture=capture)


def parse_env_assignments(values, source_label="--env"):
    env = {}
    for item in values or []:
        text = str(item).strip()
        if "=" not in text:
            raise ValueError(f"Invalid {source_label} value {text!r}; expected KEY=VALUE")
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid {source_label} value {text!r}; empty key")
        env[key] = value
    return env


def normalize_env_map(raw, source_label="meta.env"):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid {source_label}; expected object")
    out = {}
    for key, value in raw.items():
        skey = str(key).strip()
        if not skey:
            raise ValueError(f"Invalid {source_label}; contains empty key")
        out[skey] = "" if value is None else str(value)
    return out


def normalize_arg_list(raw, source_label="meta.openshot_args"):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid {source_label}; expected array")
    out = []
    for value in raw:
        text = str(value).strip()
        if text:
            out.append(text)
    return out


def resolve_openshot_root(openshot_root=None):
    if openshot_root:
        root = Path(openshot_root).expanduser().resolve()
    else:
        env_root = os.getenv("OPENSHOT_QT_ROOT", "").strip()
        if env_root:
            root = Path(env_root).expanduser().resolve()
        else:
            root = DEFAULT_OPENSHOT_ROOT
    launch_py = root / "src" / "launch.py"
    if not launch_py.exists():
        raise FileNotFoundError(
            f"OpenShot launch entrypoint not found: {launch_py}. "
            "Set --openshot-root or OPENSHOT_QT_ROOT."
        )
    return root


def launch_openshot(home_dir, extra_env=None, openshot_root=None, extra_args=None):
    openshot_root = resolve_openshot_root(openshot_root)
    launch_py = openshot_root / "src" / "launch.py"
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    if extra_env:
        for k, v in extra_env.items():
            env[str(k)] = str(v)
    cmd = ["python3", str(launch_py)]
    if extra_args:
        cmd.extend(str(x) for x in extra_args)
    return subprocess.Popen(cmd, env=env)


def reset_openshot_profile(home_dir):
    profile_dir = Path(home_dir) / ".openshot_qt"
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)


def purge_project_target(project_path):
    project_path = Path(project_path)
    if project_path.exists():
        project_path.unlink()

    stem = project_path.stem
    assets_dir = project_path.parent / f"{stem}_assets"
    if assets_dir.exists() and assets_dir.is_dir():
        shutil.rmtree(assets_dir)


def close_app(proc):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def wait_for_window(name, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = xdotool("search", "--name", name, check=False, capture=True)
        if proc.returncode == 0 and proc.stdout.strip():
            ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if ids:
                return ids[-1]
        time.sleep(0.2)
    raise TimeoutError(f"Could not find window matching name '{name}' in {timeout}s")


def focus_window(window_id):
    xdotool("windowactivate", "--sync", str(window_id))
    xdotool("windowraise", str(window_id))


def maximize_window(window_id):
    try:
        wmctrl("-ir", str(window_id), "-b", "add,maximized_vert,maximized_horz")
    except Exception:
        # Fallback if wmctrl behavior differs under a specific WM.
        xdotool("windowsize", str(window_id), "100%", "100%", check=False)
    xdotool("windowmove", str(window_id), "0", "0", check=False)


def get_window_pid(window_id):
    proc = xdotool("getwindowpid", str(window_id), check=False, capture=True)
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    return int(raw) if raw.isdigit() else None


def get_active_window_id():
    active = xdotool("getactivewindow", check=False, capture=True)
    if active.returncode != 0 or not active.stdout.strip():
        return None
    return active.stdout.strip()


def get_focused_window_pid():
    active_id = get_active_window_id()
    if not active_id:
        return None
    return get_window_pid(active_id)


def get_window_name(window_id):
    proc = xdotool("getwindowname", str(window_id), check=False, capture=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def get_window_class(window_id):
    """Return WM_CLASS string for an X11 window, if available."""
    proc = run_cmd(["xprop", "-id", str(window_id), "WM_CLASS"], check=False, capture=True)
    if proc.returncode != 0:
        return ""
    out = (proc.stdout or "").strip()
    if "=" not in out:
        return out
    return out.split("=", 1)[1].strip().strip('"')


class ReplayAbort(Exception):
    pass


class EmergencyStop:
    def __init__(self):
        self._stop = threading.Event()
        self._listener = None

    @property
    def triggered(self):
        return self._stop.is_set()

    def start(self):
        try:
            from pynput import keyboard
        except ModuleNotFoundError:
            self._listener = None
            print("Warning: pynput unavailable; emergency Esc stop disabled.")
            return

        def on_press(key):
            k = str(key)
            vk = getattr(key, "vk", None)
            if k == "Key.esc" or vk == 27:
                self._stop.set()
                return False
            return True

        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()


def guarded_sleep(seconds, emergency_stop):
    remaining = float(seconds)
    while remaining > 0:
        if emergency_stop and emergency_stop.triggered:
            raise ReplayAbort("Emergency Esc pressed")
        chunk = min(0.05, remaining)
        time.sleep(chunk)
        remaining -= chunk


def ensure_safe_focus(
    expected_pid=None,
    expected_window_id=None,
    allow_foreign_dialogs=False,
    foreign_dialog_pattern=None,
):
    # Window-manager transitions (opening/closing native dialogs) can briefly
    # report no active/focused window. Retry shortly before failing.
    active_id = None
    focused_pid = None
    for _ in range(20):
        active_id = get_active_window_id()
        focused_pid = get_focused_window_pid()
        if active_id is not None and focused_pid is not None:
            break
        time.sleep(0.02)

    if active_id is None:
        raise ReplayAbort("Cannot determine active window id")
    if focused_pid is None:
        raise ReplayAbort("Cannot determine focused window PID")
    if expected_pid is not None and int(focused_pid) != int(expected_pid):
        raise ReplayAbort(
            f"Focused window PID {focused_pid} does not match OpenShot PID {expected_pid}"
        )
    if expected_window_id is not None and str(active_id) != str(expected_window_id):
        # Allow child/dialog windows as long as they belong to the same OpenShot process.
        if expected_pid is None:
            raise ReplayAbort(
                f"Active window id {active_id} does not match OpenShot window id {expected_window_id}"
            )
        active_pid = get_window_pid(active_id)
        if active_pid is None or int(active_pid) != int(expected_pid):
            if not allow_foreign_dialogs:
                raise ReplayAbort(
                    f"Active window id {active_id} is not an OpenShot-owned dialog/window"
                )
            pattern = foreign_dialog_pattern or r"(open|choose|select).*(file|folder)|file.*(open|chooser)|portal"
            class_pattern = r"(portal|xdg-desktop-portal|gtkfilechooser|nautilus|org\.gnome\.Nautilus|filechooser)"

            # Some native dialogs are briefly untitled on creation. Retry title/class probes.
            wname = ""
            wclass = ""
            for _ in range(20):
                wname = get_window_name(active_id)
                wclass = get_window_class(active_id)
                if wname or wclass:
                    break
                time.sleep(0.02)

            if wname and re.search(pattern, wname, flags=re.IGNORECASE):
                return active_id
            if wclass and re.search(class_pattern, wclass, flags=re.IGNORECASE):
                return active_id
            if not wname and not wclass:
                raise ReplayAbort(
                    f"Active window id {active_id} is foreign and has no readable title/class"
                )
            raise ReplayAbort(
                f"Active foreign window rejected. title={wname!r} class={wclass!r}"
            )
    return active_id


def parse_shell_kv(stdout):
    out = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def get_window_geometry(window_id):
    proc = xdotool("getwindowgeometry", "--shell", str(window_id), check=False, capture=True)
    if proc.returncode != 0:
        return None
    m = parse_shell_kv(proc.stdout)
    try:
        return {
            "x": int(m["X"]),
            "y": int(m["Y"]),
            "width": int(m["WIDTH"]),
            "height": int(m["HEIGHT"]),
        }
    except Exception:
        return None


def get_window_frame_extents(window_id):
    """Return frame extents as dict(left,right,top,bottom) if available."""
    proc = run_cmd(["xprop", "-id", str(window_id), "_NET_FRAME_EXTENTS"], check=False, capture=True)
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if "=" not in text:
        return None
    try:
        rhs = text.split("=", 1)[1].strip()
        parts = [int(p.strip()) for p in rhs.split(",")]
        if len(parts) != 4:
            return None
        return {"left": parts[0], "right": parts[1], "top": parts[2], "bottom": parts[3]}
    except Exception:
        return None


def get_clickable_geometry(window_id):
    """Client geometry expanded with frame/titlebar extents."""
    base = get_window_geometry(window_id)
    if not base:
        return None
    ext = get_window_frame_extents(window_id) or {"left": 0, "right": 0, "top": 0, "bottom": 0}
    return {
        "x": base["x"] - int(ext.get("left", 0)),
        "y": base["y"] - int(ext.get("top", 0)),
        "width": base["width"] + int(ext.get("left", 0)) + int(ext.get("right", 0)),
        "height": base["height"] + int(ext.get("top", 0)) + int(ext.get("bottom", 0)),
    }


def get_mouse_location():
    proc = xdotool("getmouselocation", "--shell", check=False, capture=True)
    if proc.returncode != 0:
        return None
    m = parse_shell_kv(proc.stdout)
    try:
        return {"x": int(m["X"]), "y": int(m["Y"])}
    except Exception:
        return None


def point_in_window(x, y, geometry, margin=0):
    gx = geometry["x"]
    gy = geometry["y"]
    gw = geometry["width"]
    gh = geometry["height"]
    return gx - margin <= x < gx + gw + margin and gy - margin <= y < gy + gh + margin


def normalize_replay_key(key):
    """Translate recorded key token to xdotool-compatible key name."""
    if not isinstance(key, str):
        return str(key)
    aliases = {
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
    return aliases.get(key, key)


def char_to_keysym(ch):
    """Map single printable chars to xdotool-compatible keysyms when needed."""
    table = {
        "/": "slash",
        "\\": "backslash",
        "]": "bracketright",
        "[": "bracketleft",
        "-": "minus",
        "=": "equal",
        ";": "semicolon",
        "'": "apostrophe",
        ",": "comma",
        ".": "period",
        "`": "grave",
    }
    return table.get(ch, ch)


def replay_key_press(key, default_key_delay_ms, held_keys=None):
    key = normalize_replay_key(key)
    held = held_keys or set()
    control_mods = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R"}
    control_held = any(m in held for m in control_mods)

    if DEBUG_REPLAY:
        print(
            f"[DBG] key action raw={key!r} control_held={control_held} held={sorted(held)}"
        )

    # Hard-map path punctuation to X11 keysyms first.
    if key == "/" and not control_held:
        try:
            if DEBUG_REPLAY:
                print("[DBG] injecting '/' via KP_Divide")
            xdotool("key", "--clearmodifiers", "--delay", str(default_key_delay_ms), "KP_Divide")
            return
        except subprocess.CalledProcessError:
            if DEBUG_REPLAY:
                print("[DBG] KP_Divide failed, trying slash keysym")
            xdotool("key", "--clearmodifiers", "--delay", str(default_key_delay_ms), "slash")
            return
    if key == "\\" and not control_held:
        try:
            xdotool("key", "--clearmodifiers", "--delay", str(default_key_delay_ms), "backslash")
            return
        except subprocess.CalledProcessError:
            pass

    # Printable chars are typed directly only when no modifier is held.
    if isinstance(key, str) and len(key) == 1 and not control_held:
        # Use Unicode keysym to avoid keyboard-layout remapping (e.g. '/' -> 'l').
        codepoint = ord(key)
        if codepoint <= 0x10FFFF:
            if DEBUG_REPLAY:
                print(f"[DBG] injecting printable via U+{codepoint:04X}")
            xdotool("key", "--clearmodifiers", "--delay", str(default_key_delay_ms), f"U{codepoint:04X}")
        else:
            xdotool("type", "--clearmodifiers", "--delay", str(default_key_delay_ms), key)
        return

    try:
        if isinstance(key, str) and len(key) == 1 and control_held:
            key = char_to_keysym(key)
        xdotool("key", "--delay", str(default_key_delay_ms), key)
    except subprocess.CalledProcessError:
        # Fallback for punctuation/symbol tokens that xdotool key() rejects.
        if isinstance(key, str) and len(key) == 1:
            if control_held:
                keysym = char_to_keysym(key)
                if DEBUG_REPLAY:
                    print(f"[DBG] key() failed, fallback keysym={keysym}")
                xdotool("key", "--delay", str(default_key_delay_ms), keysym)
            else:
                codepoint = ord(key)
                if DEBUG_REPLAY:
                    print(f"[DBG] key() failed, fallback U+{codepoint:04X}")
                xdotool("key", "--clearmodifiers", "--delay", str(default_key_delay_ms), f"U{codepoint:04X}")
            return
        raise


def run_actions(
    actions,
    main_window_id=None,
    default_key_delay_ms=20,
    emergency_stop=None,
    expected_pid=None,
    enforce_pointer_bounds=True,
    pointer_margin=8,
    allow_foreign_dialogs=True,
    foreign_dialog_pattern=None,
    enforce_move_bounds=False,
    speed=1.0,
):
    held_keys = set()
    win_geo = get_clickable_geometry(main_window_id) if main_window_id is not None else None
    if enforce_pointer_bounds and main_window_id is not None and not win_geo:
        raise ReplayAbort("Could not determine OpenShot window geometry")

    try:
        for step in actions:
            if emergency_stop and emergency_stop.triggered:
                raise ReplayAbort("Emergency Esc pressed")

            action = step.get("action")
            if DEBUG_REPLAY:
                print(f"[DBG] step action={action} payload={step}")
            if action == "sleep":
                raw = float(step.get("seconds", 0.0))
                scaled = raw / speed if speed > 0 else raw
                guarded_sleep(scaled, emergency_stop)
            elif action == "move":
                active_id = ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds and active_id is not None:
                    win_geo = get_clickable_geometry(active_id) or win_geo
                x = int(step["x"])
                y = int(step["y"])
                if (
                    enforce_pointer_bounds
                    and enforce_move_bounds
                    and win_geo
                    and not point_in_window(x, y, win_geo, margin=pointer_margin)
                ):
                    raise ReplayAbort(
                        f"Blocked unsafe move to ({x},{y}) outside OpenShot window bounds {win_geo}"
                    )
                xdotool("mousemove", str(x), str(y))
            elif action == "click":
                active_id = ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds and win_geo:
                    win_geo = get_clickable_geometry(active_id) or win_geo
                    mouse = get_mouse_location()
                    if not mouse:
                        raise ReplayAbort("Could not determine mouse position before click")
                    if not point_in_window(mouse["x"], mouse["y"], win_geo, margin=pointer_margin):
                        raise ReplayAbort(
                            f"Blocked unsafe click at ({mouse['x']},{mouse['y']}) outside OpenShot window bounds {win_geo}"
                        )
                button = str(int(step.get("button", 1)))
                xdotool("click", button)
            elif action == "mousedown":
                active_id = ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds and win_geo:
                    win_geo = get_clickable_geometry(active_id) or win_geo
                    mouse = get_mouse_location()
                    if not mouse:
                        raise ReplayAbort("Could not determine mouse position before mousedown")
                    if not point_in_window(mouse["x"], mouse["y"], win_geo, margin=pointer_margin):
                        # Dialog/main close buttons are often in the title bar
                        # above client geometry. Allow a small title-bar band
                        # for the currently active OpenShot-owned window, and
                        # also for trusted foreign dialogs.
                        active_pid = get_window_pid(active_id)
                        is_openshot_owned = (
                            expected_pid is not None
                            and active_pid is not None
                            and int(active_pid) == int(expected_pid)
                        )
                        is_foreign_dialog = (
                            expected_pid is not None
                            and active_pid is not None
                            and int(active_pid) != int(expected_pid)
                        )
                        titlebar_margin = max(pointer_margin, 40)
                        in_titlebar_band = (
                            (win_geo["x"] - pointer_margin) <= mouse["x"] < (win_geo["x"] + win_geo["width"] + pointer_margin)
                            and (win_geo["y"] - titlebar_margin) <= mouse["y"] < win_geo["y"]
                        )
                        if not ((is_openshot_owned or is_foreign_dialog) and in_titlebar_band):
                            raise ReplayAbort(
                                f"Blocked unsafe mousedown at ({mouse['x']},{mouse['y']}) outside OpenShot window bounds {win_geo}"
                            )
                        if DEBUG_REPLAY:
                            print(
                                f"[DBG] allowing titlebar mousedown outside client bounds at "
                                f"({mouse['x']},{mouse['y']}) vs {win_geo}"
                            )
                button = str(int(step.get("button", 1)))
                xdotool("mousedown", button)
            elif action == "mouseup":
                active_id = ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds and win_geo:
                    win_geo = get_clickable_geometry(active_id) or win_geo
                    mouse = get_mouse_location()
                    if not mouse:
                        raise ReplayAbort("Could not determine mouse position before mouseup")
                    if not point_in_window(mouse["x"], mouse["y"], win_geo, margin=pointer_margin):
                        # Mouse release can legitimately occur after focus/window
                        # transitions (for example, dialog open/close during
                        # double-click flows). Releasing is lower risk than
                        # initiating a click, so allow it to avoid false aborts.
                        if DEBUG_REPLAY:
                            print(
                                f"[DBG] allowing mouseup outside bounds at "
                                f"({mouse['x']},{mouse['y']}) vs {win_geo}"
                            )
                button = str(int(step.get("button", 1)))
                xdotool("mouseup", button)
            elif action == "scroll":
                active_id = ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds and win_geo:
                    win_geo = get_clickable_geometry(active_id) or win_geo
                    mouse = get_mouse_location()
                    if not mouse:
                        raise ReplayAbort("Could not determine mouse position before scroll")
                    if not point_in_window(mouse["x"], mouse["y"], win_geo, margin=pointer_margin):
                        raise ReplayAbort(
                            f"Blocked unsafe scroll at ({mouse['x']},{mouse['y']}) outside OpenShot window bounds {win_geo}"
                        )

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

                dx = quantize(step.get("dx", 0))
                dy = quantize(step.get("dy", 0))
                if dy > 0:
                    xdotool("click", "--repeat", str(abs(dy)), "4")
                elif dy < 0:
                    xdotool("click", "--repeat", str(abs(dy)), "5")
                if dx > 0:
                    xdotool("click", "--repeat", str(abs(dx)), "7")
                elif dx < 0:
                    xdotool("click", "--repeat", str(abs(dx)), "6")
            elif action == "keydown":
                ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                key = normalize_replay_key(str(step["key"]))
                xdotool("keydown", key)
                held_keys.add(key)
            elif action == "keyup":
                ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                key = normalize_replay_key(str(step["key"]))
                xdotool("keyup", key)
                held_keys.discard(key)
            elif action == "key":
                ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                key = str(step["key"])
                replay_key_press(key, default_key_delay_ms, held_keys=held_keys)
            elif action == "type":
                ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                text = str(step["text"])
                # Replay text using hardened per-char injector to avoid layout issues.
                for ch in text:
                    replay_key_press(ch, default_key_delay_ms, held_keys=set())
            elif action == "wait_window":
                name = str(step["name"])
                timeout = float(step.get("timeout", 20.0))
                wait_for_window(name, timeout=timeout)
            elif action == "focus_main":
                if main_window_id is None:
                    raise RuntimeError("focus_main action requires main_window_id")
                focus_window(main_window_id)
                ensure_safe_focus(
                    expected_pid,
                    main_window_id,
                    allow_foreign_dialogs=allow_foreign_dialogs,
                    foreign_dialog_pattern=foreign_dialog_pattern,
                )
                if enforce_pointer_bounds:
                    win_geo = get_clickable_geometry(main_window_id) or win_geo
            else:
                raise ValueError(f"Unknown action: {action}")
    finally:
        # Always release any modifier(s) held by this replay run.
        for key in sorted(held_keys):
            xdotool("keyup", key, check=False)


def load_actions(path):
    actions, _meta = load_actions_bundle(path)
    return actions


def load_actions_bundle(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    meta = {}
    if isinstance(data, dict):
        actions = data.get("actions", [])
        meta = data.get("meta", {})
    elif isinstance(data, list):
        actions = data
    else:
        raise ValueError("Actions JSON must be an array or {'actions': [...]}")
    if not isinstance(actions, list):
        raise ValueError("'actions' must be a list")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("'meta' must be an object when present")
    return actions, meta


def main():
    parser = argparse.ArgumentParser(description="Replay UI actions via xdotool")
    parser.add_argument("--actions", required=True, help="Path to actions JSON")
    parser.add_argument(
        "--window-name",
        default="OpenShot Video Editor",
        help="Window title regex/name used to locate the main window",
    )
    parser.add_argument("--key-delay-ms", type=int, default=20)
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Replay speed multiplier. >1 is faster (sleep times are divided by this value).",
    )
    parser.add_argument(
        "--home",
        default=str((Path(__file__).resolve().parent / "artifacts" / "home").resolve()),
        help="HOME directory for OpenShot profile when launching",
    )
    parser.add_argument(
        "--openshot-root",
        default=os.getenv("OPENSHOT_QT_ROOT", str(DEFAULT_OPENSHOT_ROOT)),
        help="Path to openshot-qt repository (or set OPENSHOT_QT_ROOT).",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Attach to an already running OpenShot window instead of launching",
    )
    parser.add_argument(
        "--preserve-home",
        action="store_true",
        help="Do not reset HOME/.openshot_qt before launching OpenShot",
    )
    parser.add_argument(
        "--allow-outside-window-clicks",
        action="store_true",
        help="Disable pointer-bounds safety check (not recommended)",
    )
    parser.add_argument(
        "--pointer-margin",
        type=int,
        default=8,
        help="Extra pixels outside window bounds allowed for pointer actions",
    )
    parser.add_argument(
        "--enforce-move-bounds",
        action="store_true",
        help="Also enforce pointer-bounds check for move actions",
    )
    parser.add_argument(
        "--no-foreign-file-dialogs",
        dest="allow_foreign_file_dialogs",
        action="store_false",
        help="Disallow active non-OpenShot dialog windows entirely",
    )
    parser.add_argument(
        "--foreign-dialog-pattern",
        default=r"(open|choose|select).*(file|folder)|file.*(open|chooser)|portal",
        help="Regex for trusted non-OpenShot dialog window titles",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print verbose per-step/key replay debug logs",
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
    parser.add_argument(
        "--openshot-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument passed to openshot-qt launch.py (repeatable; use --openshot-arg=--flag=value)",
    )
    args = parser.parse_args()
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    global DEBUG_REPLAY
    DEBUG_REPLAY = bool(args.debug)

    actions, meta = load_actions_bundle(args.actions)
    recorded_env = normalize_env_map(meta.get("env"), source_label="actions meta.env")
    try:
        cli_env = parse_env_assignments(args.env)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.lang:
        cli_env["LANG"] = args.lang
        cli_env["LC_ALL"] = args.lang
    launch_env = {}
    launch_env.update(recorded_env)
    launch_env.update(cli_env)
    recorded_openshot_args = normalize_arg_list(
        meta.get("openshot_args"), source_label="actions meta.openshot_args"
    )
    launch_openshot_args = list(recorded_openshot_args) + list(args.openshot_arg)
    home_dir = Path(args.home).resolve()
    home_dir.mkdir(parents=True, exist_ok=True)

    proc = None
    estop = EmergencyStop()
    estop.start()
    try:
        if not args.no_launch:
            if not args.preserve_home:
                reset_openshot_profile(home_dir)
            proc = launch_openshot(
                home_dir,
                extra_env=launch_env or None,
                openshot_root=args.openshot_root,
                extra_args=launch_openshot_args or None,
            )
        elif launch_env or launch_openshot_args:
            print("Warning: actions metadata contains launch config, but --no-launch cannot apply it.")

        main_window = wait_for_window(args.window_name, timeout=40.0)
        focus_window(main_window)
        maximize_window(main_window)
        focus_window(main_window)

        target_pid = get_window_pid(main_window)
        if target_pid is None:
            raise SystemExit("Could not determine OpenShot window PID")
        if proc and proc.pid != target_pid:
            # In rare cases search may match an older instance.
            raise SystemExit(
                f"Window PID {target_pid} does not match launched OpenShot PID {proc.pid}"
            )
        ensure_safe_focus(
            target_pid,
            main_window,
            allow_foreign_dialogs=args.allow_foreign_file_dialogs,
            foreign_dialog_pattern=args.foreign_dialog_pattern,
        )

        run_actions(
            actions,
            main_window_id=main_window,
            default_key_delay_ms=args.key_delay_ms,
            emergency_stop=estop,
            expected_pid=target_pid,
            enforce_pointer_bounds=not args.allow_outside_window_clicks,
            pointer_margin=args.pointer_margin,
            allow_foreign_dialogs=args.allow_foreign_file_dialogs,
            foreign_dialog_pattern=args.foreign_dialog_pattern,
            enforce_move_bounds=args.enforce_move_bounds,
            speed=args.speed,
        )

        print(
            "Replayed actions from",
            shlex.quote(args.actions),
            "on window id",
            main_window,
            "(PID",
            target_pid,
            ")",
        )
    except ReplayAbort as exc:
        print(f"Replay aborted safely: {exc}")
        raise SystemExit(2)
    finally:
        estop.stop()
        close_app(proc)


if __name__ == "__main__":
    main()
