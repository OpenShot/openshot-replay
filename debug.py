#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SUITE_DIR = ROOT / "suite"
sys.path.insert(0, str(SUITE_DIR))

import tests as suite_tests


def normalize_case_name(value):
    raw = Path(str(value or "").strip()).name
    if raw.endswith(".actions.json"):
        return raw[: -len(".actions.json")]
    stem = Path(raw).stem
    if stem.endswith(".actions"):
        return stem[: -len(".actions")]
    return stem


def normalize_events(rows):
    filtered = [r for r in rows if r.get("event") not in {"meta", "selection"}]
    alias_map = suite_tests.AliasMap()
    out = []
    for i, row in enumerate(filtered):
        following = filtered[i + 1 :]
        has_non_dialog = any(r.get("event") != "dialog_lifecycle" for r in following)
        normalized = suite_tests.normalize_trace_event(
            row,
            alias_map,
            has_following_non_dialog=has_non_dialog,
        )
        if normalized is not None:
            out.append(normalized)
    return suite_tests.collapse_duplicate_dialog_shown(out)


def normalize_updates(rows):
    alias_map = suite_tests.AliasMap()
    updates = suite_tests.events_only(rows, "update")
    return [suite_tests.normalize_update_event(row, alias_map) for row in updates]


def normalize_selections(rows):
    alias_map = suite_tests.AliasMap()
    selections = suite_tests.events_only(rows, "selection")
    normalized = [suite_tests.normalize_selection_event(row, alias_map) for row in selections]
    return suite_tests.dedupe_selections(normalized)


def load_trace_pair(case_name, expected_root, actual_root, trace_name):
    expected_path = expected_root / f"{case_name}.{trace_name}.jsonl"
    actual_path = actual_root / f"{case_name}.actual.{trace_name}.jsonl"
    if trace_name == "events" and not actual_path.exists():
        fallback = actual_root / f"{case_name}.{trace_name}.jsonl"
        if fallback.exists():
            actual_path = fallback
    return expected_path, actual_path


def normalize_rows(trace_name, path):
    rows = suite_tests.load_jsonl(path)
    if trace_name == "events":
        return normalize_events(rows)
    if trace_name == "updates":
        return normalize_updates(rows)
    if trace_name == "selections":
        return normalize_selections(rows)
    raise ValueError(f"Unsupported trace type: {trace_name}")


def first_mismatch(expected_rows, actual_rows, float_tol):
    limit = min(len(expected_rows), len(actual_rows))
    for idx in range(limit):
        err = suite_tests.compare_subset(
            expected_rows[idx],
            actual_rows[idx],
            path=f"row[{idx + 1}]",
            float_tol=float_tol,
        )
        if err:
            return idx + 1, err
    if len(expected_rows) != len(actual_rows):
        return limit + 1, (
            f"row count mismatch: expected {len(expected_rows)}, got {len(actual_rows)}"
        )
    return None, None


def print_context(expected_rows, actual_rows, index, context):
    start = max(1, index - context)
    end = min(max(len(expected_rows), len(actual_rows)), index + context)
    for current in range(start, end + 1):
        marker = ">>" if current == index else "  "
        exp = expected_rows[current - 1] if current - 1 < len(expected_rows) else None
        act = actual_rows[current - 1] if current - 1 < len(actual_rows) else None
        print(
            f"{marker} row[{current}] "
            f"expected={suite_tests.summarize_compared_row(exp) if exp is not None else '<missing>'}"
        )
        print(
            f"{marker} row[{current}] "
            f"actual=  {suite_tests.summarize_compared_row(act) if act is not None else '<missing>'}"
        )


def inspect_trace(case_name, trace_name, expected_root, actual_root, float_tol, context):
    expected_path, actual_path = load_trace_pair(case_name, expected_root, actual_root, trace_name)
    print(f"\n[{trace_name}]")
    print(f"expected: {expected_path}")
    print(f"actual:   {actual_path}")

    if not expected_path.exists():
        print("status: missing expected trace")
        return 1
    if not actual_path.exists():
        print("status: missing actual trace")
        return 1

    expected_rows = normalize_rows(trace_name, expected_path)
    actual_rows = normalize_rows(trace_name, actual_path)
    mismatch_index, err = first_mismatch(expected_rows, actual_rows, float_tol=float_tol)

    if mismatch_index is None:
        print(f"status: match ({len(expected_rows)} rows)")
        return 0

    print(f"status: mismatch at row[{mismatch_index}]")
    print(f"detail: {err}")
    print_context(expected_rows, actual_rows, mismatch_index, context)
    if mismatch_index - 1 < len(expected_rows):
        print("\nexpected row:")
        print(json.dumps(expected_rows[mismatch_index - 1], indent=2, sort_keys=True))
    if mismatch_index - 1 < len(actual_rows):
        print("\nactual row:")
        print(json.dumps(actual_rows[mismatch_index - 1], indent=2, sort_keys=True))
    return 1


def main():
    parser = argparse.ArgumentParser(description="Inspect expected vs actual trace mismatches for a case")
    parser.add_argument("case", help="Case name or *.actions.json filename")
    parser.add_argument(
        "--trace",
        choices=["events", "updates", "selections", "all"],
        default="all",
        help="Trace type to inspect",
    )
    parser.add_argument(
        "--expected-root",
        default=str((ROOT / "suite" / "artifacts" / "traces").resolve()),
        help="Directory containing expected *.jsonl traces",
    )
    parser.add_argument(
        "--actual-root",
        default=str((ROOT / "suite" / "artifacts" / "runs").resolve()),
        help="Directory containing actual *.jsonl traces",
    )
    parser.add_argument(
        "--float-tol",
        type=float,
        default=0.05,
        help="Absolute tolerance for numeric comparisons",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=2,
        help="Rows of context before and after the mismatch",
    )
    args = parser.parse_args()

    if args.float_tol < 0:
        raise SystemExit("--float-tol must be >= 0")
    if args.context < 0:
        raise SystemExit("--context must be >= 0")

    case_name = normalize_case_name(args.case)
    expected_root = Path(args.expected_root).resolve()
    actual_root = Path(args.actual_root).resolve()
    trace_names = ["events", "updates", "selections"] if args.trace == "all" else [args.trace]

    failures = 0
    print(f"case: {case_name}")
    print(f"float_tol: {args.float_tol}")
    for trace_name in trace_names:
        failures += inspect_trace(
            case_name,
            trace_name,
            expected_root,
            actual_root,
            args.float_tol,
            args.context,
        )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
