# OrcaSlicer Profile Cleaner

CLI tool to validate, deduplicate, and clean up OrcaSlicer user profiles.

## Critical rules

- The user's live profiles at `~/Library/Application Support/OrcaSlicer/user/` are
  irreplaceable tuned calibration data — never modify them without explicit consent.
- Every mutation backs up to `_backup/` (timestamped dir) BEFORE writing and is
  gated: `clean` requires `--execute`; `fix` and `remove-printer` prompt
  interactively. `ocs restore` is the undo. Archive, never hard-delete.
- Rename machine profiles BEFORE filament/process profiles. `compatible_printers`
  references machine names by exact string, so an uncascaded machine rename breaks
  every dependent profile. The standardizer enforces machine-first ordering and
  cascades renames automatically.
- If removing a `compatible_printers` entry would leave `[]`, archive the profile
  instead of saving it. Empty means "visible to ALL printers" in OrcaSlicer —
  it is also the most common pre-existing link issue `scan` finds.
- Broken `compatible_printers` references are ERROR severity: they make filaments
  invisible for that printer (functional breakage, not cosmetic).
- Mutation code lives in `cleaner.py` and `standardizer.py` (renames + cascades),
  plus the legacy `remove-printer` handler in `cli.py`. Put new mutations in
  `cleaner.py` or `standardizer.py`, always backup-then-write.

## Project Structure

```
orcaslicer_cleaner/
  cli.py          - Click CLI entry point (commands: scan, clean, remove-printer, fix, diff, restore)
  models.py       - Data models (Profile, ProfileInfo, ValidationIssue, DuplicateGroup)
  loader.py       - Discovers and parses .info/.json profile pairs from disk
  validators.py   - Validation checks (orphans, broken refs w/ near-match suggestion, stale, malformed JSON)
  deduplicator.py - Fuzzy name matching (rapidfuzz) + content hash dedup
  reporter.py     - Rich tables/panels for terminal output + JSON export
  cleaner.py      - Backup/archive/delete/link-audit operations with dry-run support
  standardizer.py - Name normalization (layer heights, hyphens, abbreviations, HW injection, machine rename cascade, process model-naming)
  system_profiles.py - Read-only system profile name loader from OrcaSlicer app bundle
tests/
  test_standardizer.py, test_machine_matching.py - run with `pytest`
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
pip install -e .    # installs deps + the `ocs` entry point (no extras defined)
# or: pip install -r requirements.txt && python -m orcaslicer_cleaner <command>
pytest              # run the test suite
```

```bash
ocs scan                                    # full analysis (validation, dupes, links, naming)
ocs scan --min-severity error               # only show errors
ocs scan --json-output                      # machine-readable report
ocs scan --stale-days 180                   # stale threshold in days (default 365)
ocs clean                                   # preview all deletable profiles
ocs clean --type stale --execute            # archive one category
                                            #   types: stale, invalid, dupes, orphaned-hw, broken-inherits
ocs clean --printer Doomcube                # limit to one printer (--exclude-printer skips one)
ocs remove-printer "Machine Name"           # DESTRUCTIVE: delete a machine + its exclusively-linked
                                            #   filament/process profiles; strips it from shared ones (interactive)
ocs fix                                     # interactive: remap refs, fix links, standardize names
ocs fix --only remap                        # remap/remove broken compatible_printers printer names
ocs fix --only links                        # fix empty/mismatched compatible_printers
ocs fix --only names                        # standardize names (machines first, cascade automatic)
ocs diff "Profile A" "Profile B"            # compare two profiles; fuzzy "did you mean?" on miss
ocs diff --category process A B             # disambiguate names that exist in multiple categories
ocs restore                                 # list available backups
ocs restore 20260425_1104                   # restore a backup (timestamp prefix match ok)
ocs restore <ts> --profile "Name"           # restore a single profile from a backup
```

Global flags `--profile-dir` / `--system-profiles` override the default paths;
`clean` and `fix` accept `--backup-dir`; `restore --force` skips confirmation.

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
- The `standardizer.py` hyphen rule only touches hyphens with existing whitespace
  on at least one side, preserving compound words like "V-Core" and "ASA-CF"
- Nozzle sizes in machine names (`0.4mm`) must match exactly what filament
  profiles reference. The layer-height padding rule only applies to values at
  the START of a name (process profiles); nozzle sizes at the end are never
  padded. `ocs scan` flags mismatches as ERROR with a "did you mean?" suggestion.
- Process profiles are broadened to ALL machine variants of a given model+nozzle
  when renamed. E.g., renaming to `(Doomcube - 0.4mm)` sets compatible_printers
  to both `Doomcube - LGX Lite Pro - TeaKettle - 0.4mm` and `Doomcube - WWBMG - TeaKettle - 0.4mm`.

## Conventions

- Python 3.11+
- Use dataclasses for models, not Pydantic
- Default to dry-run for any destructive operation
