#!/usr/bin/env python3
import argparse
import json
import os
import re
import time
from pathlib import Path

from cleanup import cleanup_home_artifacts
from replay import (
    EmergencyStop,
    ReplayAbort,
    close_app,
    focus_window,
    get_window_pid,
    launch_openshot,
    load_actions_bundle,
    maximize_window,
    normalize_arg_list,
    normalize_env_map,
    parse_env_assignments,
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
        return key.endswith("_id") or key in {
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
            # Raw object "id" values are volatile across runs and not semantically
            # meaningful for behavioral trace assertions.
            if k == "id":
                continue
            if is_id_like_key(k) and isinstance(v, str):
                out[k] = alias_map.alias(v)
            else:
                out[k] = normalize_ids(v, alias_map)
        return out
    if isinstance(obj, list):
        return [normalize_ids(x, alias_map) for x in obj]
    return obj


def normalize_volatile_paths(obj):
    if isinstance(obj, dict):
        return {k: normalize_volatile_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_volatile_paths(x) for x in obj]
    if isinstance(obj, str):
        return re.sub(
            r"([/\\\\][^/\\\\]+_assets[/\\\\]thumbnail[/\\\\])[^/\\\\]+",
            r"\1<THUMBNAIL>",
            obj,
        )
    return obj


def normalize_update_event(row, alias_map):
    key = normalize_ids(row.get("key"), alias_map)
    value = normalize_volatile_paths(normalize_ids(row.get("value"), alias_map))
    old_values = normalize_volatile_paths(normalize_ids(row.get("old_values"), alias_map))
    return {
        "action_type": row.get("action_type"),
        "key": key,
        "value": value,
        "old_values": old_values,
    }


def normalize_selection_event(row, alias_map):
    selected_items = normalize_ids(row.get("selected_items", []), alias_map)
    selected_tracks = normalize_ids(row.get("selected_tracks", []), alias_map)
    payload = {
        "selected_items": selected_items,
        "selected_clips_count": len(row.get("selected_clips", [])),
        "selected_transitions_count": len(row.get("selected_transitions", [])),
        "selected_effects_count": len(row.get("selected_effects", [])),
        "selected_tracks": selected_tracks,
        "selected_markers_count": len(row.get("selected_markers", [])),
        "show_property_type": row.get("show_property_type"),
    }
    return payload


def normalize_dialog_window_title(title):
    if not isinstance(title, str):
        return title
    # Export progress dialog title includes runtime-dependent FPS, which
    # fluctuates between runs and is not meaningful for behavior assertions.
    normalized = re.sub(r"\(\d+(?:\.\d+)? FPS\)", "(FPS)", title)
    # Elapsed duration can vary by small amounts across runs.
    normalized = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "H:MM:SS", normalized)
    return normalized


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
            "window_title": normalize_dialog_window_title(row.get("window_title", "")),
            "modal": bool(row.get("modal", False)),
        }
        if "result" in row:
            out["result"] = row.get("result")
        return out
    if event_name == "dock_visibility":
        object_name = row.get("object_name", "")
        # Tutorial dock visibility can change at shutdown depending on focus and
        # window-manager timing; this is not meaningful for replay correctness.
        if object_name == "dockTutorial":
            return None
        return {
            "event": "dock_visibility",
            "object_name": object_name,
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


def collapse_duplicate_dialog_shown(rows):
    out = []
    for row in rows:
        if (
            row.get("event") == "dialog_lifecycle"
            and row.get("phase") == "shown"
            and out
            and out[-1].get("event") == "dialog_lifecycle"
            and out[-1].get("phase") == "shown"
            and out[-1].get("class_name", "") == row.get("class_name", "")
            and out[-1].get("object_name", "") == row.get("object_name", "")
            and out[-1].get("window_title", "") == row.get("window_title", "")
        ):
            # Window managers/toolkits can emit duplicate "shown" lifecycle
            # events back-to-back for the same dialog.
            continue
        out.append(row)
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


def summarize_compared_row(row):
    if isinstance(row, dict):
        if "event" in row:
            return summarize_event(row)
        if "action_type" in row and "key" in row:
            return f"update:{row.get('action_type')}:{row.get('key')}"
        if "selected_items" in row:
            return (
                "selection:"
                f"items={len(row.get('selected_items', []))},"
                f"clips={row.get('selected_clips_count', '?')},"
                f"tracks={len(row.get('selected_tracks', []))}"
            )
    try:
        text = json.dumps(row, sort_keys=True, ensure_ascii=True)
    except TypeError:
        text = repr(row)
    if len(text) > 220:
        return text[:217] + "..."
    return text


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


def describe_count_mismatch(event_label, expected_rows, actual_rows, float_tol):
    min_len = min(len(expected_rows), len(actual_rows))
    for idx in range(min_len):
        err = compare_subset(
            expected_rows[idx],
            actual_rows[idx],
            path=f"{event_label}[{idx + 1}]",
            float_tol=float_tol,
        )
        if err:
            hints = [
                f"first divergence at {event_label}[{idx + 1}]",
                f"expected={summarize_compared_row(expected_rows[idx])}",
                f"actual={summarize_compared_row(actual_rows[idx])}",
            ]
            # Shift-detection hints for likely missing/extra rows around the first divergence.
            if idx + 1 < len(actual_rows):
                if (
                    compare_subset(expected_rows[idx], actual_rows[idx + 1], float_tol=float_tol)
                    is None
                ):
                    hints.append(
                        f"likely unexpected {event_label} at actual[{idx + 1}]: "
                        f"{summarize_compared_row(actual_rows[idx])}"
                    )
            if idx + 1 < len(expected_rows):
                if (
                    compare_subset(expected_rows[idx + 1], actual_rows[idx], float_tol=float_tol)
                    is None
                ):
                    hints.append(
                        f"likely missing {event_label} at expected[{idx + 1}]: "
                        f"{summarize_compared_row(expected_rows[idx])}"
                    )
            return f"{err}; " + "; ".join(hints)

    if len(expected_rows) > len(actual_rows):
        first_missing = len(actual_rows) + 1
        return (
            f"first missing {event_label} at expected[{first_missing}]: "
            f"{summarize_compared_row(expected_rows[first_missing - 1])}"
        )
    if len(actual_rows) > len(expected_rows):
        first_unexpected = len(expected_rows) + 1
        return (
            f"first unexpected {event_label} at actual[{first_unexpected}]: "
            f"{summarize_compared_row(actual_rows[first_unexpected - 1])}"
        )
    return "unable to isolate mismatch detail"


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


def _build_match_matrix(expected_rows, actual_rows, float_tol):
    matrix = []
    for expected in expected_rows:
        row = []
        for actual in actual_rows:
            row.append(compare_subset(expected, actual, path="window", float_tol=float_tol) is None)
        matrix.append(row)
    return matrix


def _find_perfect_matching(matrix):
    n = len(matrix)
    if n == 0:
        return []
    if any(not any(row) for row in matrix):
        return None

    match_to_expected = [-1] * n

    def dfs(expected_idx, visited_actual):
        for actual_idx, ok in enumerate(matrix[expected_idx]):
            if not ok or visited_actual[actual_idx]:
                continue
            visited_actual[actual_idx] = True
            owner = match_to_expected[actual_idx]
            if owner == -1 or dfs(owner, visited_actual):
                match_to_expected[actual_idx] = expected_idx
                return True
        return False

    for expected_idx in range(n):
        visited_actual = [False] * n
        if not dfs(expected_idx, visited_actual):
            return None

    expected_to_actual = [-1] * n
    for actual_idx, expected_idx in enumerate(match_to_expected):
        expected_to_actual[expected_idx] = actual_idx
    if any(x < 0 for x in expected_to_actual):
        return None
    return expected_to_actual


def try_reordered_window_match(expected_rows, actual_rows, start_idx, float_tol, max_window=20):
    remaining = min(len(expected_rows) - start_idx, len(actual_rows) - start_idx, max_window)
    if remaining < 2:
        return 0

    for size in range(2, remaining + 1):
        exp_chunk = expected_rows[start_idx : start_idx + size]
        act_chunk = actual_rows[start_idx : start_idx + size]
        matrix = _build_match_matrix(exp_chunk, act_chunk, float_tol=float_tol)
        matching = _find_perfect_matching(matrix)
        if matching is not None:
            return size
    return 0


def assert_updates_trace(expected_path, actual_path, float_tol=0.05):
    exp_rows = events_only(load_jsonl(expected_path), "update")
    act_rows = events_only(load_jsonl(actual_path), "update")
    if len(exp_rows) != len(act_rows):
        details = describe_count_mismatch("update", exp_rows, act_rows, float_tol=float_tol)
        raise AssertionError(
            f"update event count mismatch: expected {len(exp_rows)}, got {len(act_rows)}; {details}"
        )

    exp_alias = AliasMap()
    act_alias = AliasMap()
    exp_norm = [normalize_update_event(e, exp_alias) for e in exp_rows]
    act_norm = [normalize_update_event(a, act_alias) for a in act_rows]

    assertion_count = 0
    idx = 0
    while idx < len(exp_norm):
        e_norm = exp_norm[idx]
        a_norm = act_norm[idx]
        err = compare_subset(e_norm, a_norm, path=f"update[{idx + 1}]", float_tol=float_tol)
        if err:
            consumed = try_reordered_window_match(
                exp_norm,
                act_norm,
                idx,
                float_tol=float_tol,
                max_window=20,
            )
            if consumed <= 0:
                raise AssertionError(err)
            for j in range(idx, idx + consumed):
                assertion_count += count_leaf_assertions(exp_norm[j])
            idx += consumed
            continue
        assertion_count += count_leaf_assertions(e_norm)
        idx += 1
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
        details = describe_count_mismatch("selection", exp_rows, act_rows, float_tol=float_tol)
        raise AssertionError(
            f"selection event count mismatch: expected {len(exp_rows)}, got {len(act_rows)}; {details}"
        )
    assertion_count = 0
    idx = 0
    while idx < len(exp_rows):
        e = exp_rows[idx]
        a = act_rows[idx]
        err = compare_subset(e, a, path=f"selection[{idx + 1}]", float_tol=float_tol)
        if err:
            consumed = try_reordered_window_match(
                exp_rows,
                act_rows,
                idx,
                float_tol=float_tol,
                max_window=20,
            )
            if consumed <= 0:
                raise AssertionError(err)
            for j in range(idx, idx + consumed):
                assertion_count += count_leaf_assertions(exp_rows[j])
            idx += consumed
            continue
        assertion_count += count_leaf_assertions(e)
        idx += 1
    return {"events": len(exp_rows), "assertions": assertion_count}


def is_clip_update_burst_event(row):
    if not isinstance(row, dict):
        return False
    if row.get("event") != "update":
        return False
    if row.get("action_type") not in {"insert", "update", "delete"}:
        return False
    key = row.get("key")
    return (
        isinstance(key, list)
        and len(key) == 2
        and key[0] == "clips"
        and isinstance(key[1], dict)
        and not key[1]
    )


def burst_end(rows, start_idx):
    idx = start_idx
    while idx < len(rows) and is_clip_update_burst_event(rows[idx]):
        idx += 1
    return idx


def assert_unordered_clip_update_burst(expected_rows, actual_rows, base_idx, float_tol):
    if len(expected_rows) != len(actual_rows):
        raise AssertionError(
            "clip-update burst size mismatch at "
            f"event[{base_idx + 1}]: expected {len(expected_rows)}, got {len(actual_rows)}; "
            f"expected_first={summarize_compared_row(expected_rows[0]) if expected_rows else 'n/a'}; "
            f"actual_first={summarize_compared_row(actual_rows[0]) if actual_rows else 'n/a'}"
        )

    unmatched_actual = list(range(len(actual_rows)))
    assertion_count = 0
    for exp_offset, expected in enumerate(expected_rows):
        assertion_count += count_leaf_assertions(expected)
        matched_idx = None
        for pos, actual_idx in enumerate(unmatched_actual):
            actual = actual_rows[actual_idx]
            err = compare_subset(
                expected,
                actual,
                path=f"event[{base_idx + exp_offset + 1}]",
                float_tol=float_tol,
            )
            if err is None:
                matched_idx = pos
                break
        if matched_idx is None:
            candidates = ", ".join(
                summarize_compared_row(actual_rows[i]) for i in unmatched_actual[:3]
            )
            raise AssertionError(
                "clip-update burst mismatch at "
                f"event[{base_idx + exp_offset + 1}]: "
                f"no matching actual event for expected={summarize_compared_row(expected)}; "
                f"remaining_actual={candidates or 'none'}"
            )
        unmatched_actual.pop(matched_idx)
    return assertion_count


def assert_events_trace(expected_path, actual_path, float_tol=0.05):
    # Selection events are validated separately by assert_selections_trace().
    # Excluding them here avoids duplicate checks and ordering noise.
    exp_rows = [r for r in load_jsonl(expected_path) if r.get("event") not in {"meta", "selection"}]
    act_rows = [r for r in load_jsonl(actual_path) if r.get("event") not in {"meta", "selection"}]

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
    exp_norm = collapse_duplicate_dialog_shown(exp_norm)
    act_norm = collapse_duplicate_dialog_shown(act_norm)

    if len(exp_norm) != len(act_norm):
        details = describe_count_mismatch("event", exp_norm, act_norm, float_tol=float_tol)
        raise AssertionError(
            f"events count mismatch: expected {len(exp_norm)}, got {len(act_norm)} "
            f"(raw expected={len(exp_rows)}, raw actual={len(act_rows)}); {details}"
        )

    assertion_count = 0
    idx = 0
    while idx < len(exp_norm):
        e = exp_norm[idx]
        a = act_norm[idx]

        if is_clip_update_burst_event(e) and is_clip_update_burst_event(a):
            exp_end = burst_end(exp_norm, idx)
            act_end = burst_end(act_norm, idx)
            assertion_count += assert_unordered_clip_update_burst(
                exp_norm[idx:exp_end],
                act_norm[idx:act_end],
                idx,
                float_tol=float_tol,
            )
            idx = exp_end
            continue

        err = compare_subset(e, a, path=f"event[{idx + 1}]", float_tol=float_tol)
        if err:
            consumed = try_reordered_window_match(
                exp_norm,
                act_norm,
                idx,
                float_tol=float_tol,
                max_window=20,
            )
            if consumed <= 0:
                prev_idx = idx - 1
                prev_ctx = ""
                if prev_idx >= 0:
                    prev_ctx = f"; previous matched event: {summarize_event(exp_norm[prev_idx])}"
                raise AssertionError(
                    f"{err}. expected={summarize_event(e)} actual={summarize_event(a)}{prev_ctx}"
                )
            for j in range(idx, idx + consumed):
                assertion_count += count_leaf_assertions(exp_norm[j])
            idx += consumed
            continue
        assertion_count += count_leaf_assertions(e)
        idx += 1
    return {"events": len(exp_norm), "assertions": assertion_count}


def derive_expected_trace_paths(actions_file):
    base = Path(actions_file).name
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


def build_case_from_actions(actions_file):
    actions_file = Path(actions_file).resolve()
    expected_updates, expected_selections, expected_events = derive_expected_trace_paths(actions_file)
    case_name = actions_file.stem.replace(".actions", "")
    return {
        "name": case_name,
        "actions_file": actions_file,
        "assert_updates": True,
        "assert_selections": True,
        "assert_events": expected_events.exists(),
        "expected_updates": expected_updates,
        "expected_selections": expected_selections,
        "expected_events": expected_events,
    }


def discover_cases(cases_dir):
    actions_files = sorted(cases_dir.glob("*.actions.json"))
    if not actions_files:
        return []

    cases = []
    for actions_file in actions_files:
        cases.append(build_case_from_actions(actions_file))
    return cases


def normalize_case_selector(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("--case cannot be empty")
    name = Path(raw).name
    if name.endswith(".actions.json"):
        return name[: -len(".actions.json")]
    stem = Path(name).stem
    if stem.endswith(".actions"):
        return stem[: -len(".actions")]
    return stem


def filter_cases(cases, selectors):
    if not selectors:
        return cases

    selected_names = []
    seen = set()
    for selector in selectors:
        case_name = normalize_case_selector(selector)
        if case_name not in seen:
            selected_names.append(case_name)
            seen.add(case_name)

    cases_by_name = {case["name"]: case for case in cases}
    missing = [name for name in selected_names if name not in cases_by_name]
    if missing:
        available = ", ".join(sorted(cases_by_name.keys()))
        raise SystemExit(
            f"Unknown --case value(s): {', '.join(missing)}. "
            f"Available cases: {available}"
        )
    return [cases_by_name[name] for name in selected_names]


def print_results_table(rows):
    headers = ["Case", "Result", "Time (s)", "Assertions", "Events", "Updates", "Selections", "Details"]
    widths = [len(h) for h in headers]

    normalized = []
    for row in rows:
        values = [
            str(row.get("name", "")),
            str(row.get("result", "")),
            str(row.get("elapsed", "")),
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


def run_case(
    case,
    home_dir,
    output_dir,
    window_name,
    speed,
    openshot_root,
    extra_env,
    extra_openshot_args,
    emergency_stop=None,
):
    cleanup = cleanup_home_artifacts(home_dir, remove_profile=True, remove_exports=True)
    print(
        f"[CLEANUP] {case['name']}: removed OpenShot profile dir '{cleanup['profile_dir']}' "
        f"(if present) and {cleanup['artifacts_removed']} home artifact(s)"
    )
    actual_updates = output_dir / f"{case['name']}.actual.updates.jsonl"
    actual_selections = output_dir / f"{case['name']}.actual.selections.jsonl"
    actual_events = output_dir / f"{case['name']}.actual.events.jsonl"
    for p in (actual_updates, actual_selections, actual_events):
        if p.exists():
            p.unlink()

    actions, meta = load_actions_bundle(case["actions_file"])
    recorded_env = normalize_env_map(meta.get("env"), source_label=f"{case['actions_file']} meta.env")
    recorded_openshot_args = normalize_arg_list(
        meta.get("openshot_args"), source_label=f"{case['actions_file']} meta.openshot_args"
    )
    launch_openshot_args = list(recorded_openshot_args) + list(extra_openshot_args or [])
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

    proc = launch_openshot(
        home_dir,
        extra_env=env,
        openshot_root=openshot_root,
        extra_args=launch_openshot_args or None,
    )
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
            emergency_stop=emergency_stop,
        )
    finally:
        close_app(proc)

    return actual_updates, actual_selections, actual_events


def main():
    parser = argparse.ArgumentParser(description="Run replay cases and trace-based assertions")
    parser.add_argument(
        "--cases",
        default=str((Path(__file__).resolve().parent / "cases").resolve()),
        help="Directory containing *.actions.json",
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
    parser.add_argument(
        "--openshot-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra argument passed to openshot-qt launch.py for every case (repeatable; use --openshot-arg=--flag=value)",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="CASE",
        help="Run only selected case name(s) or *.actions.json filename(s) (repeatable)",
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
    cli_openshot_args = normalize_arg_list(args.openshot_arg, source_label="--openshot-arg")

    cases_dir = Path(args.cases).resolve()
    home_dir = Path(args.home).resolve()
    output_dir = Path(args.out).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)

    cases = discover_cases(cases_dir)
    if not cases:
        raise SystemExit(f"No *.actions.json files found in {cases_dir}")
    try:
        cases = filter_cases(cases, args.case)
    except ValueError as exc:
        raise SystemExit(str(exc))

    failures = []
    passes = 0
    total_assertions = 0
    total_trace_events = 0
    total_update_events = 0
    total_selection_events = 0
    case_rows = []
    aborted_reason = ""
    suite_start = time.perf_counter()
    estop = EmergencyStop()
    estop.start()
    try:
        for case in cases:
            case_start = time.perf_counter()
            if estop.triggered:
                aborted_reason = "Emergency Esc pressed before starting next case."
                print(f"[ABORT] {aborted_reason}")
                break
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
                    cli_openshot_args,
                    emergency_stop=estop,
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
                    f"(time={time.perf_counter() - case_start:.2f}s, "
                    f"assertions={case_assertions}, "
                    f"events={event_stats['events']}, "
                    f"updates={update_stats['events']}, "
                    f"selections={selection_stats['events']})"
                )
                case_rows.append(
                    {
                        "name": case["name"],
                        "result": "PASS",
                        "elapsed": f"{time.perf_counter() - case_start:.2f}",
                        "assertions": case_assertions,
                        "events": event_stats["events"],
                        "updates": update_stats["events"],
                        "selections": selection_stats["events"],
                        "details": "",
                    }
                )
                passes += 1
            except ReplayAbort as exc:
                if estop.triggered:
                    aborted_reason = f"Emergency Esc pressed during case '{case['name']}'."
                    print(f"[ABORT] {aborted_reason}")
                    break
                failures.append((case["name"], str(exc)))
                case_rows.append(
                    {
                        "name": case["name"],
                        "result": "FAIL",
                        "elapsed": f"{time.perf_counter() - case_start:.2f}",
                        "assertions": 0,
                        "events": 0,
                        "updates": 0,
                        "selections": 0,
                        "details": str(exc),
                    }
                )
                print(f"[FAIL] {case['name']}: {exc}")
            except KeyboardInterrupt:
                aborted_reason = "Interrupted by keyboard."
                print(f"[ABORT] {aborted_reason}")
                break
            except Exception as exc:
                failures.append((case["name"], str(exc)))
                case_rows.append(
                    {
                        "name": case["name"],
                        "result": "FAIL",
                        "elapsed": f"{time.perf_counter() - case_start:.2f}",
                        "assertions": 0,
                        "events": 0,
                        "updates": 0,
                        "selections": 0,
                        "details": str(exc),
                    }
                )
                print(f"[FAIL] {case['name']}: {exc}")
    finally:
        estop.stop()

    print_results_table(case_rows)

    print("\nSummary:")
    print(f"  Total: {len(cases)}")
    print(f"  Passed: {passes}")
    print(f"  Failed: {len(failures)}")
    print(f"  Assertions: {total_assertions}")
    print(f"  Trace events checked: {total_trace_events}")
    print(f"  Update events checked: {total_update_events}")
    print(f"  Selection events checked: {total_selection_events}")
    print(f"  Elapsed: {time.perf_counter() - suite_start:.2f}s")
    if aborted_reason:
        print(f"  Aborted: yes ({aborted_reason})")

    if failures:
        print("\nFailures:")
        for name, err in failures:
            print(f"- {name}: {err}")
        raise SystemExit(1)
    if aborted_reason:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
