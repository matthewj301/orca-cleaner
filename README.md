# OrcaSlicer Profile Cleaner

A CLI tool to wrangle the chaos of OrcaSlicer user profiles.

I built this for my own setup — 7 printers, 300+ profiles, years of "I'll clean this up later" accumulation. If you've got a similar mess of stale profiles, broken references, duplicate filaments with `-beta-v2-final-FINAL` suffixes, and profiles showing up under the wrong printer... this might help you too. Or at least give you a starting point to tweak for your own needs.

## What it does

- **Scan** your profiles for issues: broken references, stale profiles, malformed JSON, duplicate setting IDs, broken inherits chains
- **Detect duplicates** using exact content hashing and domain-aware name variation detection (understands that "0.24mm - Whistles" and "0.24mm - Whistles - Beta" are related, but "ABS - 20K" and "ABS - 50K" are not)
- **Audit printer links** — finds filament/process profiles with empty or mismatched `compatible_printers` (like your Positron filament showing up under your Bambu)
- **Fix things interactively** — remap broken printer references, fix `compatible_printers`, standardize naming (layer height padding, abbreviation expansion, hardware injection from printer associations)
- **Clean up** by archiving stale, invalid, duplicate, or orphaned-hardware profiles (always backs up first)
- **Diff** any two profiles side-by-side
- **Restore** from timestamped backups if anything goes wrong

## Install

```bash
git clone https://github.com/yourusername/orcaslicer-cleaner.git
cd orcaslicer-cleaner
pip install -r requirements.txt
```

Or if you want the `ocs` shortcut:

```bash
pip install -e .
```

## Usage

```bash
# Scan everything — validation, duplicates, link issues, naming
ocs scan
ocs scan --min-severity error       # just the serious stuff

# Interactively fix things (broken refs, mismatched links, naming)
ocs fix
ocs fix --only remap                # just fix broken printer references
ocs fix --only links                # just fix compatible_printers
ocs fix --only names                # just standardize names

# Archive/delete profiles by category
ocs clean                                        # preview everything
ocs clean --type stale --execute                 # archive stale profiles
ocs clean --type dupes --execute                 # archive exact content duplicates
ocs clean --type orphaned-hw --execute           # profiles for hardware you no longer have
ocs clean --type broken-inherits --execute       # profiles inheriting from missing presets
ocs clean --type stale --exclude-printer Positron  # skip specific printers

# Compare two profiles
ocs diff "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm)" "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm) - beta"

# Restore from backup
ocs restore                         # list available backups
ocs restore 20260425_143022         # restore a specific backup
```

Without `pip install -e .`, use `python -m orcaslicer_cleaner` instead of `ocs`.

By default it looks for profiles at `~/Library/Application Support/OrcaSlicer/user/` (macOS). Use `--profile-dir` to point elsewhere.

## How profiles work (for the curious)

Each OrcaSlicer profile is a pair of files — `Name.info` (metadata) and `Name.json` (settings). They live in `<user_dir>/<user_id>/{filament,machine,process}/`.

The tool understands the domain model:
- **Filament profiles** are hardware-specific — tuned for a particular extruder + hotend combo. Same material through different hardware = different profile.
- **Process profiles** define how the machine moves — generally filament-agnostic but printer-specific.
- **Machine profiles** define the printer itself.

This matters for duplicate detection. Two filament profiles with the same material but different hardware in their names are *not* duplicates, even if the names are similar.

## Naming conventions

The tool expects (and can standardize toward) this naming pattern:

- Filament: `Material - Brand (Extruder - Hotend - NozzleSize)`
- Process: `LayerHeight - Purpose (Extruder - Hotend - NozzleSize)`
- Machine: `PrinterModel - Extruder - Hotend - NozzleSize`

Your naming doesn't have to match exactly — the tool does its best with whatever it finds.

## Safety

- Every destructive operation defaults to dry-run (preview only)
- `--execute` is required to actually change anything
- All deletions are archived to a timestamped `_backup/` directory first
- `ocs restore` can undo any cleanup
- The tool will never touch your live profiles without explicit confirmation

## Requirements

- Python 3.11+
- click, rich, rapidfuzz (see `requirements.txt`)
- macOS assumed for default paths, but `--profile-dir` works anywhere
