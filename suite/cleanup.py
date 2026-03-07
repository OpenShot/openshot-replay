#!/usr/bin/env python3
import shutil
from pathlib import Path

DEFAULT_EXPORT_PATTERNS = ("*.mp4", "*.mp3", "*.webm", "*.mkv", "*.mov", "*.avi")
DEFAULT_HOME_PROJECT_PATTERNS = ("*.osp",)


def cleanup_home_artifacts(
    home_dir,
    *,
    remove_profile=True,
    remove_exports=True,
    export_patterns=DEFAULT_EXPORT_PATTERNS,
    remove_project_files=True,
    project_file_patterns=DEFAULT_HOME_PROJECT_PATTERNS,
    remove_project_assets=True,
):
    home_path = Path(home_dir).resolve()
    profile_dir = home_path / ".openshot_qt"
    profile_removed = False
    exports_removed = 0
    project_files_removed = 0
    project_assets_removed = 0

    if remove_profile and profile_dir.exists():
        shutil.rmtree(profile_dir)
        profile_removed = True

    if remove_exports:
        for pattern in export_patterns:
            for export_file in home_path.glob(pattern):
                if export_file.is_file() or export_file.is_symlink():
                    export_file.unlink()
                    exports_removed += 1

    if remove_project_files:
        for pattern in project_file_patterns:
            for project_file in home_path.glob(pattern):
                if project_file.is_file() or project_file.is_symlink():
                    project_file.unlink()
                    project_files_removed += 1

    if remove_project_assets:
        for assets_dir in home_path.glob("*_assets"):
            if assets_dir.is_dir() and not assets_dir.is_symlink():
                shutil.rmtree(assets_dir)
                project_assets_removed += 1

    artifacts_removed = exports_removed + project_files_removed + project_assets_removed

    return {
        "home_dir": home_path,
        "profile_dir": profile_dir,
        "profile_removed": profile_removed,
        "exports_removed": exports_removed,
        "project_files_removed": project_files_removed,
        "project_assets_removed": project_assets_removed,
        "artifacts_removed": artifacts_removed,
    }
