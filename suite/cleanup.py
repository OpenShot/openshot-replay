#!/usr/bin/env python3
import shutil
from pathlib import Path

DEFAULT_EXPORT_PATTERNS = ("*.mp4", "*.mp3", "*.webm", "*.mkv", "*.mov", "*.avi")


def cleanup_home_artifacts(
    home_dir,
    *,
    remove_profile=True,
    remove_exports=True,
    export_patterns=DEFAULT_EXPORT_PATTERNS,
):
    home_path = Path(home_dir).resolve()
    profile_dir = home_path / ".openshot_qt"
    profile_removed = False
    exports_removed = 0

    if remove_profile and profile_dir.exists():
        shutil.rmtree(profile_dir)
        profile_removed = True

    if remove_exports:
        for pattern in export_patterns:
            for export_file in home_path.glob(pattern):
                if export_file.is_file() or export_file.is_symlink():
                    export_file.unlink()
                    exports_removed += 1

    return {
        "home_dir": home_path,
        "profile_dir": profile_dir,
        "profile_removed": profile_removed,
        "exports_removed": exports_removed,
    }
