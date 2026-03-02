#!/usr/bin/env python3
import argparse
import os
import re
import shlex
import time
from pathlib import Path

from assertions import assert_baseline_contract, load_osp
from replay import (
    close_app,
    focus_window,
    get_window_pid,
    launch_openshot,
    load_actions,
    maximize_window,
    purge_project_target,
    reset_openshot_profile,
    run_actions,
    get_active_window_id,
    get_window_name,
    wait_for_window,
)


def save_project_as(baseline_path):
    # Ctrl+Shift+S, type path, Enter
    from replay import xdotool

    def active_title():
        wid = get_active_window_id()
        if not wid:
            return ""
        return get_window_name(wid) or ""

    def wait_for_title(pattern, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = active_title()
            if re.search(pattern, t, flags=re.IGNORECASE):
                return t
            time.sleep(0.05)
        return active_title()

    def open_save_dialog():
        xdotool("key", "--delay", "20", "ctrl+shift+s")
        title = wait_for_title(r"(save|untitled)", timeout=2.2)
        if re.search(r"(save|untitled)", title or "", flags=re.IGNORECASE):
            return True
        if re.search(r"(open|import|choose|select)", title or "", flags=re.IGNORECASE):
            xdotool("key", "Escape", check=False)
            time.sleep(0.2)
        xdotool("key", "alt+f", check=False)
        time.sleep(0.2)
        xdotool("key", "a", check=False)
        title = wait_for_title(r"(save|untitled)", timeout=2.2)
        return bool(re.search(r"(save|untitled)", title or "", flags=re.IGNORECASE))

    def wait_for_file(target, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if target.exists():
                return True
            time.sleep(0.1)
        return False

    purge_project_target(baseline_path)

    for _attempt in range(2):
        if not open_save_dialog():
            title = active_title()
            raise RuntimeError(f"Could not open Save Project dialog. Active window title: {title!r}")

        xdotool("key", "ctrl+l", check=False)
        time.sleep(0.12)
        xdotool("type", "--delay", "5", str(baseline_path))
        xdotool("key", "Return")
        time.sleep(0.25)
        xdotool("key", "Return", check=False)

        if wait_for_file(baseline_path, timeout=6.0):
            return

        xdotool("key", "Escape", check=False)
        time.sleep(0.2)

    raise RuntimeError(f"Save Project did not create baseline file: {baseline_path}")


def main():
    parser = argparse.ArgumentParser(description="Create baseline OpenShot project via replay")
    parser.add_argument("--setup", required=True, help="Setup actions JSON")
    parser.add_argument("--baseline", required=True, help="Baseline .osp path")
    parser.add_argument(
        "--home",
        default=str((Path(__file__).resolve().parent / "artifacts" / "home").resolve()),
        help="HOME directory used for isolated OpenShot profile",
    )
    parser.add_argument(
        "--openshot-root",
        default=os.getenv("OPENSHOT_QT_ROOT", ""),
        help="Path to openshot-qt repository (or set OPENSHOT_QT_ROOT).",
    )
    parser.add_argument("--window-name", default="OpenShot Video Editor")
    args = parser.parse_args()

    setup_path = Path(args.setup).resolve()
    baseline_path = Path(args.baseline).resolve()
    home_dir = Path(args.home).resolve()
    home_dir.mkdir(parents=True, exist_ok=True)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)

    reset_openshot_profile(home_dir)
    proc = launch_openshot(home_dir, openshot_root=args.openshot_root or None)
    try:
        window_id = wait_for_window(args.window_name, timeout=40.0)
        focus_window(window_id)
        maximize_window(window_id)
        focus_window(window_id)
        target_pid = get_window_pid(window_id)
        actions = load_actions(str(setup_path))
        run_actions(actions, main_window_id=window_id, expected_pid=target_pid, pointer_margin=56)
        save_project_as(baseline_path)
    finally:
        close_app(proc)

    if not baseline_path.exists():
        raise SystemExit(f"Baseline was not saved: {baseline_path}")

    baseline = load_osp(baseline_path)
    assert_baseline_contract(baseline)
    print("Baseline created and validated:", shlex.quote(str(baseline_path)))
    print("Isolated HOME:", shlex.quote(str(home_dir)))


if __name__ == "__main__":
    main()
