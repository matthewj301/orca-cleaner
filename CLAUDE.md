# OrcaSlicer Profile Cleaner

CLI tool to validate, deduplicate, and clean up OrcaSlicer user profiles.

## Critical rules

- The user's live profiles at `~/Library/Application Support/OrcaSlicer/user/` are
  irreplaceable tuned calibration data — never modify them without explicit consent.
- NEVER mutate profiles while OrcaSlicer is running. The app's sync engine
  treats externally-modified profiles as conflicts and DELETES them (writes
  `sync_info = delete` to the .info and removes the .json) — this silently
  destroyed 20 profiles and several machines on 2026-07-10. Mutating commands
  refuse to run against the default profile dir while the app is open
  (`_ensure_app_closed` in cli.py); recovery from such deletions = restore the
  .json AND rewrite `sync_info = delete` -> `update`, with the app closed.
- Deleting a machine in the OrcaSlicer UI produces the SAME delete-signature
  (writes `sync_info = delete`, removes the `.json`) and makes dependent
  filament/process profiles vanish from any sibling printer in the app view —
  even though their on-disk `compatible_printers` are untouched. Recover by
  restoring the machine `.json` from the off-disk git snapshot
  (`~/git/orcaslicer-profiles-backup`, which diffs cleanly against live) and
  rewriting `sync_info = delete` -> `update`, app closed. To retire a machine,
  prefer `ocs remove-printer` over UI deletion — it cascades the links safely.
- Cloning a machine in the UI does NOT copy filament/process links: those
  profiles reference the OLD machine name by exact string in
  `compatible_printers`, so the new machine sees nothing. To give a new machine
  its sibling's profiles, APPEND the new machine name to `compatible_printers`
  on every profile bound to the source machine (append, never replace — the old
  machine must keep working). `cleaner.execute_link_fixes` is the safe primitive
  (backup + atomic write + `fix-links` manifest, so `ocs undo` works); there is
  no dedicated CLI command for this yet (candidate feature: `clone-printer-links`).
- Every mutation backs up to `_backup/` (timestamped dir) BEFORE writing and is
  gated: `clean` requires `--execute`; `fix` and `remove-printer` prompt
  interactively. `ocs restore` is the undo. Archive, never hard-delete.
- All backup/write plumbing goes through `fileops.py`: atomic JSON writes
  (temp file + os.replace), collision-free timestamped backup dirs, and a
  per-backup `manifest.json` mapping each backed-up file to its original
  absolute path. `restore` uses the manifest to return files to their original
  user root and original name (even collision-suffixed copies), falls back to
  the first user root for pre-manifest backups, and backs up anything it
  overwrites into a new timestamped dir.
- Rename machine profiles BEFORE filament/process profiles. `compatible_printers`
  references machine names by exact string, so an uncascaded machine rename breaks
  every dependent profile. The standardizer enforces machine-first ordering and
  cascades renames automatically.
- If removing a `compatible_printers` entry would leave `[]`, archive the profile
  instead of saving it. Empty means "visible to ALL printers" in OrcaSlicer —
  it is also the most common pre-existing link issue `scan` finds.
- Broken `compatible_printers` references are ERROR severity: they make filaments
  invisible for that printer (functional breakage, not cosmetic).
- Mutation code lives in `cleaner.py` (including printer removal) and
  `standardizer.py` (renames + cascades); `cli.py` only gathers input and
  confirms. Put new mutations in `cleaner.py` or `standardizer.py`, always
  backup-then-write via the `fileops.py` helpers.
- Renames are pre-flighted: actions whose target name already exists on disk
  or collides with another action in the batch are skipped, so a rename can
  never leave a split profile (`New.info` + `Old.json`).

## Project Structure

```
orcaslicer_cleaner/
  cli.py          - Click CLI entry point (commands: scan, clean, remove-printer, fix, diff, matrix, restore, undo)
  models.py       - Data models (Profile, ProfileInfo, ValidationIssue, DuplicateGroup)
  loader.py       - Discovers and parses .info/.json profile pairs from disk
  validators.py   - Validation checks (orphans, broken refs w/ near-match suggestion, stale, malformed JSON)
  deduplicator.py - Fuzzy name matching (rapidfuzz) + content hash dedup
  matrix.py       - Read-only material x printer / process x model coverage matrices
  reporter.py     - Rich tables/panels for terminal output + JSON export
  cleaner.py      - Backup/archive/delete/link-audit/printer-removal operations with dry-run support
  standardizer.py - Name normalization (layer heights, hyphens, abbreviations, HW injection, machine rename cascade, process model-naming)
  fileops.py      - Atomic JSON writes, timestamped backup dirs, backup manifests w/ operation provenance
  safety.py       - Blast-radius assessment + coverage snapshot/diff for guarding mutations
  system_profiles.py - Read-only system profile name loader from OrcaSlicer app bundle
tests/
  test_standardizer.py, test_machine_matching.py, test_mutations.py - run with `pytest`
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
ocs fix                                     # interactive: remap refs, fix links, resolve dupes, standardize names
ocs fix --only remap                        # remap/remove broken compatible_printers printer names
ocs fix --only links                        # fix empty/mismatched compatible_printers, then
                                            #   interactively assign hint-less empty-cp profiles
                                            #   (suggestions from each profile's inherits value)
ocs fix --only dupes                        # resolve duplicate groups: pick keeper, archive rest,
                                            #   merge compatible_printers for "mergeable" groups
ocs fix --only names                        # standardize names (machines first, cascade automatic)
ocs matrix                                  # read-only coverage matrix (filament + process)
ocs matrix --category filament              # material/brand x machine matrix only
ocs diff "Profile A" "Profile B"            # compare two profiles; fuzzy "did you mean?" on miss
ocs diff --category process A B             # disambiguate names that exist in multiple categories
ocs undo                                    # restore the most recent backup (undo last operation)
ocs restore                                 # list available backups (shows which operation made each)
ocs restore 20260425_1104                   # restore a backup (timestamp prefix match ok)
ocs restore <ts> --profile "Name"           # restore a single profile from a backup
ocs prune-backups --keep 20                 # preview deletion of old timestamped backups
                                            #   (--execute + typed "yes"; curated dirs never touched)
```

Global flags `--profile-dir` / `--system-profiles` override the default paths;
`clean` and `fix` accept `--backup-dir`; `restore --force` skips confirmation.

## Domain Model

- Filament profiles are hardware-specific: tuned per extruder + hotend combo.
  Same material through different hardware = different profile, NOT a duplicate.
- Duplicate classification: the content hash EXCLUDES the "name" field (it
  mirrors the filename) and compatible_printers. Identical content + identical
  printers = "exact_content"; identical content + different printers =
  "mergeable" (resolved in `fix --only dupes` by keeping one profile with the
  union of printers).
- Safety rails (do not weaken): every mutation batch is pre-flighted by
  `safety.assess_blast_radius` — plans that touch a machine, archive >=3 AND
  >15% of a category, or archive >=20 profiles require a typed "yes" instead
  of y/n. After every mutation the CLI reloads and reports coverage lost per
  printer + newly broken references, with an `ocs undo` hint. Backup manifests
  record which command created them (fileops.create_backup_dir operation arg).
- DATA-LOSS GUARDS (from the 2026-07-10 incident — do not weaken):
  `clean --type dupes` auto-archives ONLY name-variations (beta/copy/v2) of
  the keeper. Identical content under structurally different names (e.g.
  different hardware in the name) may be the only copy serving another
  printer — interactive review only. Machines NEVER participate in
  exact-content dedup (they routinely differ only by name, and other profiles
  reference them by name). The nozzle minimal-form rename rule applies only
  at the very END of a name or trailing parenthetical — mid-name mm values
  are layer heights in non-convention names.
- Process profiles are printer-model-specific but filament-agnostic: determined by
  machine motion capability, not what filament is loaded or which extruder/hotend
  is installed. A process profile applies to ALL hardware variants of a printer model.
  Naming convention: `<layer>mm - <purpose> (<PrinterModel> - <NozzleSize>)`
- Machine profiles define the printer with hardware: "PrinterModel - Extruder - Hotend - NozzleSize"

## Off-disk backup

`~/git/orcaslicer-profiles-backup` git-snapshots the live `user/` dir via
`snapshot.sh` (rsync + commit-if-changed + push). Scheduled weekly by the
launchd agent `com.mjohnson.orcaslicer-backup` (plist staged in that repo's
`launchd/`). This protects against failures the tool's own `_backup/` can't
(disk loss, OrcaSlicer bugs, deletion outside the tool).

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
  profiles reference. Two mirror-image rules: layer heights at the START of a
  name are padded to two decimals (`0.2mm` -> `0.20mm`); nozzle sizes anywhere
  ELSE drop trailing zeros (`0.40mm` -> `0.4mm`). `ocs scan` flags mismatches
  as ERROR with a "did you mean?" suggestion.
- Duplicate keeper recommendation: for exact_content/mergeable groups the
  convention-following name wins (recency tie-break) — updated_time is only
  sync time and identical bytes make it meaningless; for beta/test variant
  groups most-recently-updated wins (latest tune).
- Process profiles are broadened to ALL machine variants of a given model+nozzle
  when renamed. E.g., renaming to `(Doomcube - 0.4mm)` sets compatible_printers
  to both `Doomcube - LGX Lite Pro - TeaKettle - 0.4mm` and `Doomcube - WWBMG - TeaKettle - 0.4mm`.

## Conventions

- Python 3.11+
- Use dataclasses for models, not Pydantic
- Default to dry-run for any destructive operation
