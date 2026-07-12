"""Shared file operations: atomic JSON writes, timestamped backups, manifests.

Every mutation in this project goes through these helpers so that:
- JSON writes are atomic (temp file + os.replace) and can't corrupt a profile
  if interrupted mid-write.
- Every backed-up file is recorded in a per-backup manifest.json mapping the
  backup copy to its original absolute path, so `ocs restore` can put files
  back where they came from (including collision-suffixed copies).
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

MANIFEST_NAME = "manifest.json"


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically via a temp file in the same directory."""
    text = json.dumps(data, indent=4, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def create_backup_dir(backup_root: Path, operation: str | None = None) -> Path:
    """Create and return a fresh timestamped backup directory under backup_root.

    Two operations in the same second get distinct directories so their
    backups (and manifests) never merge. `operation` records provenance in
    the manifest (which command created this backup, with argv and time) so
    forensics never has to infer it from file patterns.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_root / timestamp
    counter = 1
    while backup_dir.exists():
        backup_dir = backup_root / f"{timestamp}_{counter}"
        counter += 1
    backup_dir.mkdir(parents=True)
    if operation:
        atomic_write_json(backup_dir / MANIFEST_NAME, {
            "operation": {
                "command": operation,
                "argv": sys.argv[1:],
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
            },
            "files": {},
        })
    return backup_dir


def mirror_backup_dir(backup_dir: Path, mirror_root: Path) -> Path | None:
    """Copy a completed timestamped backup into an additional root.

    `ocs restore`/`ocs undo` only ever read the default `_backup` root, so the
    real backup always lives there; a user-supplied `--backup-dir` is honored as
    an *extra* self-contained copy (manifest included) rather than a replacement.
    This keeps undo working no matter what flags a mutation was run with.

    The mirror keeps the same timestamped directory name; on the rare collision
    a numeric suffix is appended, matching create_backup_dir's scheme. Returns
    the mirror path, or None if the copy failed (mirroring is best-effort — a
    failed extra copy must never abort the mutation whose real backup succeeded).
    """
    try:
        mirror_root.mkdir(parents=True, exist_ok=True)
        dest = mirror_root / backup_dir.name
        counter = 1
        while dest.exists():
            dest = mirror_root / f"{backup_dir.name}_{counter}"
            counter += 1
        shutil.copytree(backup_dir, dest)
        return dest
    except OSError:
        return None


def load_operation(backup_dir: Path) -> str | None:
    """Return the operation label recorded in a backup's manifest, if any."""
    op = _load_manifest_data(backup_dir).get("operation", {})
    return op.get("command") if isinstance(op, dict) else None


def _load_manifest_data(backup_dir: Path) -> dict:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_manifest(backup_dir: Path) -> dict[str, str]:
    """Return mapping of backup-relative path -> original absolute path."""
    files = _load_manifest_data(backup_dir).get("files", {})
    return files if isinstance(files, dict) else {}


def load_renames(backup_dir: Path) -> dict[str, str]:
    """Return mapping of old absolute path -> new absolute path for files
    renamed during the operation this backup belongs to. Restore uses this
    to remove the new-name files when putting the originals back."""
    renames = _load_manifest_data(backup_dir).get("renames", {})
    return renames if isinstance(renames, dict) else {}


def _update_manifest(backup_dir: Path, section: str, key: str, value: str) -> None:
    data = _load_manifest_data(backup_dir)
    if not isinstance(data.get(section), dict):
        data[section] = {}
    data[section][key] = value
    atomic_write_json(backup_dir / MANIFEST_NAME, data)


def _record_manifest(backup_dir: Path, rel_path: str, original_path: Path) -> None:
    _update_manifest(backup_dir, "files", rel_path, str(original_path))


def record_rename(backup_dir: Path, old_path: Path, new_path: Path) -> None:
    """Record that old_path was renamed to new_path during this operation."""
    _update_manifest(backup_dir, "renames", str(old_path), str(new_path))


def _collision_free_dest(backup_dir: Path, category: str, filename: str) -> Path:
    category_dir = backup_dir / category
    category_dir.mkdir(parents=True, exist_ok=True)
    dst = category_dir / filename
    stem, suffix = dst.stem, dst.suffix
    counter = 1
    while dst.exists():
        dst = category_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return dst


def backup_copy(src: Path, backup_dir: Path, category: str) -> Path | None:
    """Copy src into backup_dir/category and record it in the manifest.

    Returns the backup path, or None if src doesn't exist.
    """
    if not src.exists():
        return None
    dst = _collision_free_dest(backup_dir, category, src.name)
    shutil.copy2(str(src), str(dst))
    _record_manifest(backup_dir, f"{category}/{dst.name}", src)
    return dst


def backup_move(src: Path, backup_dir: Path, category: str) -> Path | None:
    """Move src into backup_dir/category and record it in the manifest.

    Returns the backup path, or None if src doesn't exist.
    """
    if not src.exists():
        return None
    dst = _collision_free_dest(backup_dir, category, src.name)
    shutil.move(str(src), str(dst))
    _record_manifest(backup_dir, f"{category}/{dst.name}", src)
    return dst
