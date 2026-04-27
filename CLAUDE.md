# OrcaSlicer Profile Cleaner

CLI tool to validate, deduplicate, and clean up OrcaSlicer user profiles.

## Project Structure

```
orcaslicer_cleaner/
  cli.py          - Click CLI entry point (commands: scan, clean, fix, diff, restore)
  models.py       - Data models (Profile, ProfileInfo, ValidationIssue, DuplicateGroup)
  loader.py       - Discovers and parses .info/.json profile pairs from disk
  validators.py   - Validation checks (orphans, broken refs, stale, malformed JSON)
  deduplicator.py - Fuzzy name matching (rapidfuzz) + content hash dedup
  reporter.py     - Rich tables/panels for terminal output + JSON export
  cleaner.py      - Backup/archive/delete/link-audit operations with dry-run support
  standardizer.py - Name normalization (layer heights, hyphens, abbreviations, HW injection)
  system_profiles.py - Read-only system profile name loader from OrcaSlicer app bundle
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
# or: pip install -r requirements.txt && python -m orcaslicer_cleaner <command>

ocs scan                                    # full analysis (validation, dupes, links, naming)
ocs scan --min-severity error               # only show errors
ocs clean                                   # preview all deletable profiles
ocs clean --type stale --execute            # archive stale profiles
ocs clean --type orphaned-hw                # preview profiles for removed hardware
ocs clean --type broken-inherits            # preview broken inherits
ocs clean --exclude-printer Positron        # skip a printer
ocs fix                                     # interactive: remap refs, fix links, standardize names
ocs fix --only remap                        # just fix broken printer references
ocs fix --only links                        # just fix compatible_printers
ocs fix --only names                        # just standardize names
ocs diff "Profile A" "Profile B"            # compare two profiles
ocs restore                                 # list available backups
ocs restore <timestamp>                     # restore from backup
```

## Conventions

- Python 3.11+
- Use dataclasses for models, not Pydantic
- All file mutations go through cleaner.py — never modify files in other modules
- Always back up before deleting (archive to _backup/ directory)
- Default to dry-run for any destructive operation
- User's live profiles at ~/Library/Application Support/OrcaSlicer/user/ — never modify without explicit consent
