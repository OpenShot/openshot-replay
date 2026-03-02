#!/usr/bin/env python3
import argparse
import json
import os
import re
from pathlib import Path

from replay import (
    close_app,
    focus_window,
    get_window_pid,
    launch_openshot,
    load_actions_bundle,
    maximize_window,
    normalize_env_map,
    parse_env_assignments,
    reset_openshot_profile,
    run_actions,
    wait_for_window,
)


def load_jsonl(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def events_only(rows, event_name):
    return [r for r in rows if r.get("event") == event_name]


class AliasMap:
    def __init__(self):
        self._next = 1
        self._map = {}

    def alias(self, value):
        if not isinstance(value, str):
            return value
        if value not in self._map:
            self._map[value] = f"id_{self._next}"
            self._next += 1
        return self._map[value]


def normalize_ids(obj, alias_map):
    def is_id_like_key(key):
        if not isinstance(key, str):
            return False
        return key == "id" or key.endswith("_id") or key in {
            "parentObjectId",
            "file_id",
            "clip_id",
            "effect_id",
            "transition_id",
            "marker_id",
            "layer_id",
        }

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_id_like_key(k) and isinstance(v, str):
                out[k] = alias_map.alias(v)
            else:
                out[k] = normalize_ids(v, alias_map)
        return out
    if isinstance(obj, list):
        return [normalize_ids(x, alias_map) for x in obj]
    return obj


def normalize_update_event(row, alias_map):
    key = normalize_ids(row.get("key"), alias_map)
    value = normalize_ids(row.get("value"), alias_map)
    old_values = normalize_ids(row.get("old_values"), alias_map)
    return {
        "action_type": row.get("action_type"),
        "key": key,
        "value": value,
        "old_values": old_values,
    }


def normalize_selection_event(row, alias_map):
    def alias_list(values):
        out = []
        for v in values:
            if isinstance(v, str):
                out.append(alias_map.alias(v))
            else:
                out.append(v)
        return out

    payload = {
        "selected_items": normalize_ids(row.get("selected_items", []), alias_map),
        "selected_clips": alias_list(row.get("selected_clips", [])),
        "selected_transitions": alias_list(row.get("selected_transitions", [])),
        "selected_effects": alias_list(row.get("selected_effects", [])),
        "selected_tracks": alias_list(row.get("selected_tracks", [])),
        "selected_markers": alias_list(row.get("selected_markers", [])),
    }
    return payload


def normalize_trace_event(row, alias_map, has_following_non_dialog=False):
    event_name = row.get("event")
    if event_name == "update":
        payload = normalize_update_event(row, alias_map)
        payload["event"] = "update"
        return payload
    if event_name == "selection":
        payload = normalize_selection_event(row, alias_map)
        payload["event"] = "selection"
        return payload
    if event_name == "action_triggered":
        return {
            "event": "action_triggered",
            "action_name": row.get("action_name", ""),
            "action_text": row.get("action_text", ""),
            "checked": bool(row.get("checked", False)),
        }
    if event_name == "dialog_lifecycle":
        phase = row.get("phase", "")
        # Ignore highly noisy dialog phases that do not encode user intent.
        if phase in {"hidden", "closed", "finished"}:
            return None
        # Ignore trailing accept/reject that can be emitted only during forced shutdown.
        if phase in {"accepted", "rejected"} and not has_following_non_dialog:
            return None
        out = {
            "event": "dialog_lifecycle",
            "phase": phase,
            "class_name": row.get("class_name", ""),
            "object_name": row.get("object_name", ""),
            "window_title": row.get("window_title", ""),
            "modal": bool(row.get("modal", False)),
        }
        if "result" in row:
            out["result"] = row.get("result")
        return out
    if event_name == "dock_visibility":
        return {
            "event": "dock_visibility",
            "object_name": row.get("object_name", ""),
            "window_title": row.get("window_title", ""),
            "visible": bool(row.get("visible", False)),
            "floating": bool(row.get("floating", False)),
        }
    if event_name == "cache_progress":
        out = {"event": "cache_progress"}
        for key in (
            "current_frame",
            "cache_class",
            "metrics",
            "preview_cache_files",
            "preview_cache_bytes",
        ):
            if key in row:
                out[key] = normalize_ids(row.get(key), alias_map)
        return out

    # Fallback for unknown event types: drop volatile fields but keep payload.
    out = {}
    for key, value in row.items():
        if key in {"seq", "ts", "pid", "data_version", "project_path"}:
            continue
        out[key] = normalize_ids(value, alias_map)
    if "event" not in out:
        out["event"] = event_name or "unknown"
    return out


def summarize_event(row):
    event_name = row.get("event", "unknown")
    if event_name == "update":
        return f"update:{row.get('action_type')}:{row.get('key')}"
    if event_name == "selection":
        return "selection"
    if event_name == "action_triggered":
        return f"action:{row.get('action_name') or row.get('action_text')}"
    if event_name == "dialog_lifecycle":
        return f"dialog:{row.get('phase')}:{row.get('window_title')}"
    if event_name == "dock_visibility":
        return f"dock:{row.get('object_name') or row.get('window_title')}:{row.get('visible')}"
    if event_name == "cache_progress":
        return f"cache:frame={row.get('current_frame')}"
    return event_name


def compare_subset(expected, actual, path="root", float_tol=0.05):
    def as_number(value):
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", text):
                try:
                    return float(text)
                except ValueError:
                    return None
        return None

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{path}: expected dict, got {type(actual).__name__}"
        for k, v in expected.items():
            if k not in actual:
                return f"{path}: missing key '{k}'"
            err = compare_subset(v, actual[k], f"{path}.{k}", float_tol=float_tol)
            if err:
                return err
        return None
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return f"{path}: expected list, got {type(actual).__name__}"
        if len(expected) != len(actual):
            return f"{path}: expected list length {len(expected)}, got {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            err = compare_subset(e, a, f"{path}[{i}]", float_tol=float_tol)
            if err:
                return err
        return None
    exp_num = as_number(expected)
    act_num = as_number(actual)
    if exp_num is not None and act_num is not None:
        if abs(exp_num - act_num) <= float_tol:
            return None
        return (
            f"{path}: expected {expected!r}, got {actual!r} "
            f"(abs diff {abs(exp_num-act_num):.6f} > tol {float_tol})"
        )
    if expected != actual:
        return f"{path}: expected {expected!r}, got {actual!r}"
    return None


def count_leaf_assertions(value):
    if isinstance(value, dict):
        total = 0
        for v in value.values():
            total += count_leaf_assertions(v)
        return total
    if isinstance(value, list):
        total = 0
        for item in value:
            total += count_leaf_assertions(item)
        return total
    return 1


def assert_updates_trace(expected_path, actual_path, float_tol=0.05):
    exp_rows = events_only(load_jsonl(expected_path), "update")
    act_rows = events_only(load_jsonl(actual_path), "update")
    if len(exp_rows) != len(act_rows):
        raise AssertionError(
            f"update event count mismatch: expected {len(exp_rows)}, got {len(act_rows)}"
        )

    exp_alias = AliasMap()
    act_alias = AliasMap()
    assertion_count = 0
    for idx, (e, a) in enumerate(zip(exp_rows, act_rows), 1):
        e_norm = normalize_update_event(e, exp_alias)
        a_norm = normalize_update_event(a, act_alias)
        assertion_count += count_leaf_assertions(e_norm)
        err = compare_subset(e_norm, a_norm, path=f"update[{idx}]", float_tol=float_tol)
        if err:
            raise AssertionError(err)
    return {"events": len(exp_rows), "assertions": assertion_count}


def dedupe_selections(rows):
    out = []
    last = None
    for row in rows:
        sig = json.dumps(row, sort_keys=True)
        if sig != last:
            out.append(row)
            last = sig
    return out


def assert_selections_trace(expected_path, actual_path, float_tol=0.05):
    exp_raw = events_only(load_jsonl(expected_path), "selection")
    act_raw = events_only(load_jsonl(actual_path), "selection")

    exp_alias = AliasMap()
    act_alias = AliasMap()
    exp_rows = dedupe_selections([normalize_selection_event(r, exp_alias) for r in exp_raw])
    act_rows = dedupe_selections([normalize_selection_event(r, act_alias) for r in act_raw])

    if len(exp_rows) != len(act_rows):
        raise AssertionError(
            f"selection event count mismatch: expected {len(exp_rows)}, got {len(act_rows)}"
        )
    assertion_count = 0
    for idx, (e, a) in enumerate(zip(exp_rows, act_rows), 1):
        assertion_count += count_leaf_assertions(e)
        err = compare_subset(e, a, path=f"selection[{idx}]", float_tol=float_tol)
        if err:
            raise AssertionError(err)
    return {"events": len(exp_rows), "assertions": assertion_count}


def assert_events_trace(expected_path, actual_path, float_tol=0.05):
    exp_rows = [r for r in load_jsonl(expected_path) if r.get("event") != "meta"]
    act_rows = [r for r in load_jsonl(actual_path) if r.get("event") != "meta"]

    exp_alias = AliasMap()
    act_alias = AliasMap()
    exp_norm = []
    act_norm = []

    for i, row in enumerate(exp_rows):
        following = exp_rows[i + 1 :]
        has_non_dialog = any(r.get("event") != "dialog_lifecycle" for r in following)
        nrow = normalize_trace_event(row, exp_alias, has_following_non_dialog=has_non_dialog)
        if nrow is not None:
            exp_norm.append(nrow)
    for i, row in enumerate(act_rows):
        following = act_rows[i + 1 :]
        has_non_dialog = any(r.get("event") != "dialog_lifecycle" for r in following)
        nrow = normalize_trace_event(row, act_alias, has_following_non_dialog=has_non_dialog)
        if nrow is not None:
            act_norm.append(nrow)

    if len(exp_norm) != len(act_norm):
        raise AssertionError(
            f"events count mismatch: expected {len(exp_norm)}, got {len(act_norm)} "
            f"(raw expected={len(exp_rows)}, raw actual={len(act_rows)})"
        )

    assertion_count = 0
    for idx, (e, a) in enumerate(zip(exp_norm, act_norm), 1):
        assertion_count += count_leaf_assertions(e)
        err = compare_subset(e, a, path=f"event[{idx}]", float_tol=float_tol)
        if err:
            prev_idx = idx - 2
            prev_ctx = ""
            if prev_idx >= 0:
                prev_ctx = f"; previous matched event: {summarize_event(exp_norm[prev_idx])}"
            raise AssertionError(
                f"{err}. expected={summarize_event(e)} actual={summarize_event(a)}{prev_ctx}"
            )
    return {"events": len(exp_norm), "assertions": assertion_count}


def derive_expected_trace_paths(case):
    expected_updates = case.get("expected_updates")
    expected_selections = case.get("expected_selections")
    expected_events = case.get("expected_events")
    if expected_updates and expected_selections and expected_events:
        return (
            Path(expected_updates).resolve(),
            Path(expected_selections).resolve(),
            Path(expected_events).resolve(),
        )

    base = Path(case["actions_file"]).name
    if base.endswith(".actions.json"):
        base = base[: -len(".actions.json")]
    else:
        base = Path(base).stem
    root = Path(__file__).resolve().parent / "artifacts" / "traces"
    return (
        (root / f"{base}.updates.jsonl").resolve(),
        (root / f"{base}.selections.jsonl").resolve(),
        (root / f"{base}.events.jsonl").resolve(),
    )


def build_case_from_actions(actions_file, override=None):
    raw = override or {}
    actions_file = Path(actions_file).resolve()
    expected_updates, expected_selections, expected_events = derive_expected_trace_paths(
        {
            "actions_file": str(actions_file),
            "expected_updates": raw.get("expected_updates"),
            "expected_selections": raw.get("expected_selections"),
            "expected_events": raw.get("expected_events"),
        }
    )
    default_name = actions_file.stem.replace(".actions", "")
    default_assert_events = expected_events.exists()
    if "assert_events" in raw:
        default_assert_events = bool(raw["assert_events"])
    return {
        "name": raw.get("name", default_name),
        "actions_file": actions_file,
        "assert_updates": bool(raw.get("assert_updates", True)),
        "assert_selections": bool(raw.get("assert_selections", True)),
        "assert_events": default_assert_events,
        "expected_updates": expected_updates,
        "expected_selections": expected_selections,
        "expected_events": expected_events,
    }


def discover_cases(cases_dir):
    actions_files = sorted(cases_dir.glob("*.actions.json"))
    if not actions_files:
        return []

    # Optional per-case overrides (same base name), e.g. trim_clip.case.json
    overrides = {}
    for case_file in sorted(cases_dir.glob("*.case.json")):
        raw = json.loads(case_file.read_text(encoding="utf-8"))
        key = case_file.stem.replace(".case", "")
        overrides[key] = raw

    cases = []
    for actions_file in actions_files:
        key = actions_file.stem.replace(".actions", "")
        cases.append(build_case_from_actions(actions_file, override=overrides.get(key)))
    return cases


def print_results_table(rows):
    headers = ["Case", "Result", "Assertions", "Events", "Updates", "Selections", "Details"]
    widths = [len(h) for h in headers]

    normalized = []
    for row in rows:
        values = [
            str(row.get("name", "")),
            str(row.get("result", "")),
            str(row.get("assertions", "")),
            str(row.get("events", "")),
            str(row.get("updates", "")),
            str(row.get("selections", "")),
            str(row.get("details", "")),
        ]
        normalized.append(values)
        for i, value in enumerate(values):
            widths[i] = max(widths[i], len(value))

    def fmt(values):
        return "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(values)) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    print("\nResults:")
    print(sep)
    print(fmt(headers))
    print(sep)
    for values in normalized:
        print(fmt(values))
    print(sep)


def run_case(case, home_dir, output_dir, window_name, speed, openshot_root, extra_env):
    reset_openshot_profile(home_dir)
    actual_updates = output_dir / f"{case['name']}.actual.updates.jsonl"
    actual_selections = output_dir / f"{case['name']}.actual.selections.jsonl"
    actual_events = output_dir / f"{case['name']}.actual.events.jsonl"
    for p in (actual_updates, actual_selections, actual_events):
        if p.exists():
            p.unlink()

    actions, meta = load_actions_bundle(case["actions_file"])
    recorded_env = normalize_env_map(meta.get("env"), source_label=f"{case['actions_file']} meta.env")
    env = {
        "OPENSHOT_UI_TRACE": "1",
        "OPENSHOT_UI_TRACE_UPDATES": str(actual_updates),
        "OPENSHOT_UI_TRACE_SELECTIONS": str(actual_selections),
        "OPENSHOT_UI_TRACE_EVENTS": str(actual_events),
        "OPENSHOT_UI_TRACE_INCLUDE_LOAD": "0",
        "OPENSHOT_UI_TRACE_INCLUDE_IGNORED": "0",
    }
    env.update(recorded_env)
    env.update(extra_env)

    proc = launch_openshot(home_dir, extra_env=env, openshot_root=openshot_root)
    try:
        wid = wait_for_window(window_name, timeout=40.0)
        focus_window(wid)
        maximize_window(wid)
        focus_window(wid)
        target_pid = get_window_pid(wid)
        run_actions(
            actions,
            main_window_id=wid,
            expected_pid=target_pid,
            pointer_margin=56,
            speed=speed,
        )
    finally:
        close_app(proc)

    return actual_updates, actual_selections, actual_events


def main():
    parser = argparse.ArgumentParser(description="Run replay cases and trace-based assertions")
    parser.add_argument(
        "--cases",
        default=str((Path(__file__).resolve().parent / "cases").resolve()),
        help="Directory containing *.actions.json (and optional *.case.json overrides)",
    )
    parser.add_argument(
        "--home",
        default=str((Path(__file__).resolve().parent / "artifacts" / "home").resolve()),
        help="HOME directory used for isolated OpenShot profile",
    )
    parser.add_argument(
        "--out",
        default=str((Path(__file__).resolve().parent / "artifacts" / "runs").resolve()),
        help="Output directory for per-case actual trace files",
    )
    parser.add_argument("--window-name", default="OpenShot Video Editor")
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Replay speed multiplier for all cases. >1 is faster.",
    )
    parser.add_argument(
        "--float-tol",
        type=float,
        default=0.05,
        help="Absolute tolerance for numeric comparisons in trace assertions.",
    )
    parser.add_argument(
        "--openshot-root",
        default=os.getenv("OPENSHOT_QT_ROOT", ""),
        help="Path to openshot-qt repository (or set OPENSHOT_QT_ROOT).",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra environment variable for launched OpenShot in every case (repeatable)",
    )
    parser.add_argument(
        "--lang",
        default="",
        help="Convenience locale value for every case. Sets both LANG and LC_ALL.",
    )
    args = parser.parse_args()
    if args.speed <= 0:
        raise SystemExit("--speed must be > 0")
    if args.float_tol < 0:
        raise SystemExit("--float-tol must be >= 0")
    try:
        cli_env = parse_env_assignments(args.env)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if args.lang:
        cli_env["LANG"] = args.lang
        cli_env["LC_ALL"] = args.lang

    cases_dir = Path(args.cases).resolve()
    home_dir = Path(args.home).resolve()
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)

    cases = discover_cases(cases_dir)
    if not cases:
        raise SystemExit(f"No *.actions.json files found in {cases_dir}")

    failures = []
    passes = 0
    total_assertions = 0
    total_trace_events = 0
    total_update_events = 0
    total_selection_events = 0
    case_rows = []
    for case in cases:
        print(f"[RUN] {case['name']}")
        try:
            actual_updates, actual_selections, actual_events = run_case(
                case,
                home_dir,
                output_dir,
                args.window_name,
                args.speed,
                args.openshot_root or None,
                cli_env,
            )

            event_stats = {"events": 0, "assertions": 0}
            update_stats = {"events": 0, "assertions": 0}
            selection_stats = {"events": 0, "assertions": 0}

            if case["assert_events"]:
                if not case["expected_events"].exists():
                    raise AssertionError(f"Missing expected events trace: {case['expected_events']}")
                if not actual_events.exists():
                    raise AssertionError(f"Missing actual events trace: {actual_events}")
                event_stats = assert_events_trace(
                    case["expected_events"], actual_events, float_tol=args.float_tol
                )

            if case["assert_updates"]:
                if not case["expected_updates"].exists():
                    raise AssertionError(f"Missing expected updates trace: {case['expected_updates']}")
                if not actual_updates.exists():
                    raise AssertionError(f"Missing actual updates trace: {actual_updates}")
                update_stats = assert_updates_trace(
                    case["expected_updates"], actual_updates, float_tol=args.float_tol
                )

            if case["assert_selections"]:
                if not case["expected_selections"].exists():
                    raise AssertionError(f"Missing expected selections trace: {case['expected_selections']}")
                if not actual_selections.exists():
                    raise AssertionError(f"Missing actual selections trace: {actual_selections}")
                selection_stats = assert_selections_trace(
                    case["expected_selections"], actual_selections, float_tol=args.float_tol
                )

            case_assertions = (
                event_stats["assertions"] + update_stats["assertions"] + selection_stats["assertions"]
            )
            total_assertions += case_assertions
            total_trace_events += event_stats["events"]
            total_update_events += update_stats["events"]
            total_selection_events += selection_stats["events"]

            print(
                f"[PASS] {case['name']} "
                f"(assertions={case_assertions}, "
                f"events={event_stats['events']}, "
                f"updates={update_stats['events']}, "
                f"selections={selection_stats['events']})"
            )
            case_rows.append(
                {
                    "name": case["name"],
                    "result": "PASS",
                    "assertions": case_assertions,
                    "events": event_stats["events"],
                    "updates": update_stats["events"],
                    "selections": selection_stats["events"],
                    "details": "",
                }
            )
            passes += 1
        except Exception as exc:
            failures.append((case["name"], str(exc)))
            case_rows.append(
                {
                    "name": case["name"],
                    "result": "FAIL",
                    "assertions": 0,
                    "events": 0,
                    "updates": 0,
                    "selections": 0,
                    "details": str(exc),
                }
            )
            print(f"[FAIL] {case['name']}: {exc}")

    print_results_table(case_rows)

    print("\nSummary:")
    print(f"  Total: {len(cases)}")
    print(f"  Passed: {passes}")
    print(f"  Failed: {len(failures)}")
    print(f"  Assertions: {total_assertions}")
    print(f"  Trace events checked: {total_trace_events}")
    print(f"  Update events checked: {total_update_events}")
    print(f"  Selection events checked: {total_selection_events}")

    if failures:
        print("\nFailures:")
        for name, err in failures:
            print(f"- {name}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
