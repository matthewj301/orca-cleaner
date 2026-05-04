# OrcaSlicer Profile Cleaner

CLI tool to validate, deduplicate, and clean up OrcaSlicer user profiles.

## Project Structure

```
orcaslicer_cleaner/
  cli.py          - Click CLI entry point (commands: scan, clean, fix, diff, restore)
  models.py       - Data models (Profile, ProfileInfo, ValidationIssue, DuplicateGroup)
  loader.py       - Discovers and parses .info/.json profile pairs from disk
  validators.py   - Validation checks (orphans, broken refs w/ near-match suggestion, stale, malformed JSON)
  deduplicator.py - Fuzzy name matching (rapidfuzz) + content hash dedup
  reporter.py     - Rich tables/panels for terminal output + JSON export
  cleaner.py      - Backup/archive/delete/link-audit operations with dry-run support
  standardizer.py - Name normalization (layer heights, hyphens, abbreviations, HW injection, machine rename cascade, process model-naming)
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

## Domain Model

- Filament profiles are hardware-specific: tuned per extruder + hotend combo.
  Same material through different hardware = different profile, NOT a duplicate.
- Process profiles are printer-model-specific but filament-agnostic: determined by
  machine motion capability, not what filament is loaded or which extruder/hotend
  is installed. A process profile applies to ALL hardware variants of a printer model.
  Naming convention: `<layer>mm - <purpose> (<PrinterModel> - <NozzleSize>)`
- Machine profiles define the printer with hardware: "PrinterModel - Extruder - Hotend - NozzleSize"

## Key Paths (macOS)

- User profiles: `~/Library/Application Support/OrcaSlicer/user/`
- System profiles: `/Applications/OrcaSlicer.app/Contents/Resources/profiles/`
- Backups: `~/Library/Application Support/OrcaSlicer/_backup/`
- Override with `--profile-dir` and `--system-profiles` flags

## Extension Points

- Hardware aliases (cleaner.py `_HARDWARE_ALIASES`): maps abbreviations in profile
  names to machine name terms for link matching (e.g., "mako" -> "bambu")
- Name abbreviations (standardizer.py `_ABBREVIATIONS`): expanded during name
  standardization (e.g., "TK" -> "TeaKettle")

## Gotchas

- `updated_time` in .info files is cloud sync time, not last-used time — stale
  detection based on this is approximate
- Empty `compatible_printers` in a profile JSON means OrcaSlicer shows it for
  ALL printers — this is the most common link issue
- The `standardizer.py` hyphen rule only touches hyphens with existing whitespace
  on at least one side, preserving compound words like "V-Core" and "ASA-CF"
- Renaming a machine profile breaks all filament/process profiles that reference
  it via `compatible_printers` (exact string match). The standardizer handles this
  automatically by processing machines first and cascading to dependent profiles.
- Nozzle sizes in machine names (`0.4mm`) must match exactly what filament
  profiles reference. The layer-height padding rule only applies to values at
  the START of a name (process profiles); nozzle sizes at the end are never
  padded. `ocs scan` flags mismatches as ERROR with a "did you mean?" suggestion.
- Process profiles are broadened to ALL machine variants of a given model+nozzle
  when renamed. E.g., renaming to `(Doomcube - 0.4mm)` sets compatible_printers
  to both `Doomcube - LGX Lite Pro - TeaKettle - 0.4mm` and `Doomcube - WWBMG - TeaKettle - 0.4mm`.
- When removing a broken compatible_printers reference leaves a profile with
  empty `[]`, the profile must be archived — empty means "visible to ALL printers".

## Conventions

- Python 3.11+
- Use dataclasses for models, not Pydantic
- All file mutations go through cleaner.py — never modify files in other modules
- Always back up before deleting (archive to _backup/ directory)
- Default to dry-run for any destructive operation
- User's live profiles at ~/Library/Application Support/OrcaSlicer/user/ — never modify without explicit consent
- Machine profiles must be renamed before filament/process profiles to preserve
  compatible_printers integrity (standardizer enforces this ordering)
- Broken compatible_printers references are ERROR severity — they cause functional
  breakage in OrcaSlicer (filaments become "incompatible")
