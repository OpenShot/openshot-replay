#!/usr/bin/env python3
import json
from pathlib import Path


NEUTRAL_KEYS = [
    "files",
    "clips",
    "effects",
    "layers",
    "markers",
    "playhead_position",
    "profile",
    "width",
    "height",
    "fps",
    "display_ratio",
    "pixel_ratio",
]


def load_osp(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def neutral_snapshot(project):
    return {k: project.get(k) for k in NEUTRAL_KEYS}


def assert_baseline_contract(project):
    missing = [k for k in NEUTRAL_KEYS if k not in project]
    if missing:
        raise AssertionError(f"Baseline missing required keys: {missing}")
    if not isinstance(project.get("files"), list):
        raise AssertionError("Expected 'files' to be a list")
    if not isinstance(project.get("clips"), list):
        raise AssertionError("Expected 'clips' to be a list")
    if not isinstance(project.get("effects"), list):
        raise AssertionError("Expected 'effects' to be a list")
    if not isinstance(project.get("layers"), list):
        raise AssertionError("Expected 'layers' to be a list")


def assert_same_neutral_state(expected_project, actual_project):
    expected = neutral_snapshot(expected_project)
    actual = neutral_snapshot(actual_project)
    if expected != actual:
        raise AssertionError(
            "Neutral state mismatch.\n"
            f"Expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
            f"Actual: {json.dumps(actual, indent=2, sort_keys=True)}"
        )


def assert_case_expectations(project, expected):
    if not expected:
        return

    if "files_count" in expected and len(project.get("files", [])) != int(expected["files_count"]):
        raise AssertionError(
            f"Expected files_count={expected['files_count']}, got {len(project.get('files', []))}"
        )
    if "clips_count" in expected and len(project.get("clips", [])) != int(expected["clips_count"]):
        raise AssertionError(
            f"Expected clips_count={expected['clips_count']}, got {len(project.get('clips', []))}"
        )
    if "effects_count" in expected and len(project.get("effects", [])) != int(expected["effects_count"]):
        raise AssertionError(
            f"Expected effects_count={expected['effects_count']}, got {len(project.get('effects', []))}"
        )
    if "markers_count" in expected and len(project.get("markers", [])) != int(expected["markers_count"]):
        raise AssertionError(
            f"Expected markers_count={expected['markers_count']}, got {len(project.get('markers', []))}"
        )
    if "layers_count" in expected and len(project.get("layers", [])) != int(expected["layers_count"]):
        raise AssertionError(
            f"Expected layers_count={expected['layers_count']}, got {len(project.get('layers', []))}"
        )
    if "playhead_position" in expected and project.get("playhead_position") != expected["playhead_position"]:
        raise AssertionError(
            f"Expected playhead_position={expected['playhead_position']}, got {project.get('playhead_position')}"
        )
