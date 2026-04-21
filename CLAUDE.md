# OrcaSlicer Profile Cleaner

CLI tool to validate, deduplicate, and clean up OrcaSlicer user profiles.

## Project Structure

```
src/orcaslicer_cleaner/
  cli.py          - Click CLI entry point (commands: scan, validate, dedupe, clean)
  models.py       - Data models (Profile, ProfileInfo, ValidationIssue, DuplicateGroup)
  loader.py       - Discovers and parses .info/.json profile pairs from disk
  validators.py   - Validation checks (orphans, broken refs, stale, malformed JSON)
  deduplicator.py - Fuzzy name matching (rapidfuzz) + content hash dedup
  reporter.py     - Rich tables/panels for terminal output + JSON export
  cleaner.py      - Backup/archive/delete operations with dry-run support
```

## OrcaSlicer Profile Format

Each profile is a pair of files:
- `Name.info` — plain text metadata (sync_info, user_id, setting_id, base_id, updated_time)
- `Name.json` — JSON dict of slicer settings

Profiles are organized in: `<user_dir>/<user_id>/{filament,machine,process}/`

Key relationships:
- Filament/process profiles have `compatible_printers` referencing machine profile names
- Machine/process profiles have `inherits` referencing base presets

## Development

```bash
pip install -e ".[dev]"
ocs scan                    # full scan with default profile dir
ocs --profile-dir /path scan  # custom path
ocs validate                # validation only
ocs dedupe                  # duplicate detection only
ocs clean --dry-run         # preview cleanup
ocs clean --execute         # apply cleanup (backs up files first)
```

## Conventions

- Python 3.11+
- Use dataclasses for models, not Pydantic
- All file mutations go through cleaner.py — never modify files in other modules
- Always back up before deleting (archive to _backup/ directory)
- Default to dry-run for any destructive operation
- User's live profiles at ~/Library/Application Support/OrcaSlicer/user/ — never modify without explicit consent
