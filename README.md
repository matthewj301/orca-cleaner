# OrcaSlicer Profile Cleaner

A command-line tool for tidying up and keeping track of your OrcaSlicer profiles.

I built this for my own setup — 7 printers, 300+ profiles, years of "I'll clean this up later" accumulation. If you've got a similar mess of stale profiles, broken references, duplicate filaments with `-beta-v2-final-FINAL` suffixes, and profiles showing up under the wrong printer... this might help you too. Or at least give you a starting point to tweak for your own needs.

## What it does

- **Scan** your profiles for issues: broken references, stale profiles, malformed JSON, duplicate setting IDs, broken inherits chains
- **Detect duplicates** using exact content hashing and domain-aware name variation detection (understands that "0.24mm - Whistles" and "0.24mm - Whistles - Beta" are related, but "ABS - 20K" and "ABS - 50K" are not)
- **Audit printer links** — finds filament/process profiles with empty or mismatched `compatible_printers` (like your Positron filament showing up under your Bambu)
- **Fix things interactively** — remap broken printer references, fix `compatible_printers`, standardize naming (layer height padding, abbreviation expansion, hardware injection from printer associations)
- **Clean up** by archiving stale, invalid, duplicate, or orphaned-hardware profiles (always backs up first)
- **Diff** any two profiles side-by-side
- **Back up** the entire profile library on demand — a full snapshot you can take before doing anything risky (even with OrcaSlicer open)
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

## Trying it out

The friendliest way to start is to just look. `scan` reads your profiles and reports back — it never changes anything, so you can run it as often as you like:

```bash
ocs scan
```

You'll get a summary of anything that looks off: profiles pointing at printers you no longer have, near-duplicates, naming that's drifted over the years, and so on.

When you're ready to make changes, take a snapshot first so you can always get back to where you started:

```bash
ocs backup
```

Then quit OrcaSlicer (see the note just below) and try a guided fix. `fix` walks you through each issue one at a time and asks before doing anything:

```bash
ocs fix
```

Changed your mind? `ocs restore` brings a snapshot back. There's no step here you can't undo.

## Before you make changes: quit OrcaSlicer

OrcaSlicer keeps its profiles in sync with its cloud account, and it doesn't expect them to change while it's open. If a profile file changes underneath the running app, OrcaSlicer can treat it as a conflict and remove the profile. To prevent that, anything that edits your profiles will pause and ask you to quit OrcaSlicer first.

Looking and copying are always fine. `scan`, `diff`, and `backup` only read (or copy) your files, so you're welcome to run them with OrcaSlicer open. It's only the commands that change things — `fix`, `clean`, `remove-printer` — that need the app closed.

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
```

Without `pip install -e .`, use `python -m orcaslicer_cleaner` instead of `ocs`.

By default it looks for profiles at `~/Library/Application Support/OrcaSlicer/user/` (macOS). Use `--profile-dir` to point elsewhere.

## Backups and undo

Two things keep your profiles safe:

- **Automatic backups.** Every time the tool changes something, it first copies the files it's about to touch into a timestamped folder under `_backup/`. You don't have to remember to do this — it just happens.
- **Full snapshots.** `ocs backup` copies your entire profile library into `_backup/` whenever you ask. It only ever copies (it never moves or deletes), so it's safe to run any time, even with OrcaSlicer open. Taking one before a big cleanup is a good habit.

To go back, `ocs restore` lists your backups and puts one back:

```bash
ocs restore                     # see the backups you have
ocs restore 20260425_143022     # restore a specific one
ocs undo                        # shortcut for "restore the most recent backup"
```

Want a second copy somewhere else — an external drive, or a folder you back up separately? Add `--backup-dir`:

```bash
ocs backup --backup-dir /Volumes/Backup/orca
```

Your backup still lands in the usual `_backup/` folder, so `ocs restore` can always find it, and a matching copy is placed in the location you chose as well.

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
- Process: `LayerHeight - Purpose (PrinterModel - NozzleSize)`
- Machine: `PrinterModel - Extruder - Hotend - NozzleSize`

Your naming doesn't have to match exactly — the tool does its best with whatever it finds.

## Configuration

The tool ships with defaults tuned for the author's setup — including a few personal shorthands (like "TK" meaning "TeaKettle"). You can point those at your own gear with a config file. It's completely optional: without one, everything uses the built-in defaults.

Create `~/.config/orcaslicer-cleaner/config.toml` (or pass `--config path/to/config.toml`). Anything you leave out keeps its default, so you only need to write the parts you want to change. There's a fully-commented starting point in [`config.toml.example`](config.toml.example) — copy it and edit.

The most useful thing to personalize is the **vocabulary** — the shorthands you use in profile names:

```toml
# Match the informal hardware names in your profile names to your machines.
[hardware_aliases]
voron = "trident"       # a filament tagged "(Voron)" should match your Trident printer

# Short forms to expand when tidying names.
[abbreviations]
SM = "Sherpa Micro"
```

You can also describe your own name format — the order of the fields and the separators you use — so the tool understands your naming when it looks for duplicates and builds the coverage matrix:

```toml
[naming.filament]
format = "{brand} {material} [{hardware}]"
```

And you can adjust the numeric thresholds (how strict duplicate-matching is, how many days counts as "stale", and so on) or turn individual naming cleanups on or off. See the example file for the full list with explanations.

(One note: a custom format changes how the tool *understands* your names, and when tidying names with `ocs fix --only names` it uses your separator and hardware bracket — so if you write hardware in `[square]` brackets, it keeps them square. Fully reordering fields into a different layout during tidying isn't done yet.)

If you mistype a setting, the tool tells you rather than quietly ignoring it.

## Safety

This tool is careful with your profiles by design:

- Anything that could change your files previews first — you see exactly what will happen before it happens.
- Changes only go through when you ask for them (with `--execute`, or by confirming an interactive prompt).
- Nothing is ever hard-deleted. Profiles are archived to a timestamped `_backup/` folder, and `ocs restore` can bring them back.
- Before editing your profiles, the tool checks that OrcaSlicer is closed (see "Before you make changes" above).
- Your live profiles are never touched without your say-so.

## Requirements

- Python 3.11+
- click, rich, rapidfuzz (see `requirements.txt`)
- macOS assumed for default paths, but `--profile-dir` works anywhere
