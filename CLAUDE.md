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
- `restore`/`undo` ONLY ever read the default `_backup` (`<profile-dir>/../_backup`).
  So the default `_backup` is always the canonical store and is written on every
  backup regardless of flags. `--backup-dir` is an ADDITIONAL mirror, not a
  replacement (`_resolve_backup_roots` in cli.py returns (primary=`_backup`,
  mirror); each `execute_*` copies its completed timestamped dir to the mirror
  via `fileops.mirror_backup_dir`). Mirroring is best-effort — a failed extra
  copy warns but never aborts the mutation whose real backup succeeded.
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
  cli.py          - Click CLI entry point (commands: scan, clean, remove-printer, fix, diff, matrix, backup, restore, undo, prune-backups)
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
  config.py       - TOML user config (vocabulary, thresholds, naming grammar) + defaults
  naming.py       - Compiles config naming-format templates into name parsers (grammar engine)
  system_profiles.py - Read-only system profile name loader from OrcaSlicer app bundle
tests/
  test_standardizer.py, test_machine_matching.py, test_mutations.py, test_config.py, test_naming.py - run with `pytest`
```

## Configuration (config.py)

- `config.py` externalizes what used to be hardcoded, author-specific assumptions:
  the vocabulary dicts (`abbreviations`, `hardware_aliases`, `model_aliases`),
  the numeric thresholds (fuzz cutoffs, `stale_days`, blast-radius guards,
  `diff_match_cutoff`, `unassigned_group_threshold`), and the naming rules
  (format templates + `pad_layer_heights`/`trim_nozzle_zeros`/`normalize_hyphens`
  toggles). Loaded from `~/.config/orcaslicer-cleaner/config.toml` or `--config`.
- INVARIANT (do not weaken): `DEFAULT_CONFIG` reproduces the original hardcoded
  behavior exactly. The full test suite runs against defaults, so it's the
  regression net — any wiring that changes default behavior is a bug.
- Wiring pattern: entry points (`find_duplicates`, `audit_links`, `find_renames`,
  `find_unassigned`, `assess_blast_radius`, `_normalize_name`) take
  `config: Config = DEFAULT_CONFIG` and thread it to helpers; the CLI loads once
  into `ctx.obj["config"]` and passes it down (including through the `_fix_*`
  helpers). Never read config via module-level globals.
- Merge semantics: vocabulary sections REPLACE their default wholesale when
  present (every default entry is personal); `thresholds`/`naming` merge per-key.
  Unknown/mistyped keys raise `ConfigError` (surfaced as exit code 2) — never
  silently ignored.
- STATUS: naming format templates (`[naming.filament].format` etc.) now drive
  PARSING. `naming.py` compiles a format into a regex parser (`compile_grammar`,
  lru-cached on the frozen `CategoryNaming`); `deduplicator._parse_filament_name`
  /`_parse_process_name` and `matrix.py`'s row keys consume it. The default
  templates reproduce the old hardcoded regexes EXACTLY — verified two ways:
  `tests/test_naming.py` keeps the original regexes as a reference and asserts
  parity across a corpus, and a golden run over the real library confirmed
  byte-for-byte parity. Field names are semantic: filament {material}/{brand}/
  {hardware}, process {layer}/{purpose}/{hardware}; {layer}/{nozzle} must match
  `\d+\.?\d*mm`; {hardware} is a single greedy blob on parse (its sub-template is
  render-only). Compiler quirk faithfully reproduced: the last field is greedy
  (hardware captures to the LAST `)`), generic fields exclude their delimiter
  char, a pure-whitespace separator compiles to `\s+` (not `\s*`).
- RENDER side (partial): `naming.render_spec(fmt)` extracts the separator and
  hardware-bracket literals (`RenderSpec`, lru-cached) from a format template.
  `standardizer.py`'s name-building — `_append_hardware`, `_inject_hardware`,
  `_normalize_filament_paren`, `_normalize_process_paren` — is parameterized by a
  `RenderSpec` (default `_DEFAULT_FIL_SPEC`/`_DEFAULT_PROC_SPEC` reproduce the
  built-in convention), so `fix --only names` honors a configured separator and
  hardware bracket. Verified: default config reproduces the exact rename set on
  the real library byte-for-byte (golden `rename_baseline`), and a `[square]`
  format both prevents corruption (no stray `(paren)` appended to an existing
  `[bracket]`) and injects/normalizes into `[brackets]` — see
  `tests/test_standardizer.py::TestCustomBracketRendering`.
- SAFETY RAIL (do not weaken): `naming.validate_renderable(fmt)`, run at config
  load, rejects any format whose `{hardware}` field can't be built into a
  re-detectable single-character bracket — via a round-trip check (append with
  the spec, then require the spec's own detector to match). This blocks two
  name-corrupting classes at the source: a hardware field with no bracket
  (glues hardware on with no delimiter, regrows every run) and a multi-character
  bracket (single-char detector can't re-find it). `RenderSpec.has_hardware` is
  True only when a usable single-char bracket exists, so the render helpers also
  no-op defensively. `execute_renames` threads the machine separator + process
  RenderSpec into `_broaden_process_printers` so broadening works under a custom
  machine-name separator too.
- STILL TODO: full field REORDERING during standardization. The standardizer does
  targeted surgical edits (spacing, nozzle sizes, brackets), not parse→render, so
  it won't rewrite a name into a wholly different field order. A full
  parse→transform→render pipeline (using the `hardware` sub-template) would be the
  next step — deferred because the real-library evidence shows standardization is
  in-place tidying, making a full rewrite high-risk for low incremental value.

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
ocs backup                                  # snapshot the ENTIRE library into a timestamped _backup/
                                            #   dir (copy-only, safe while OrcaSlicer is open; restore
                                            #   with 'ocs restore'). --backup-dir ADDS a mirror copy
                                            #   (default _backup is always written so restore finds it)
ocs undo                                    # restore the most recent backup (undo last operation)
ocs restore                                 # list available backups (shows which operation made each)
ocs restore 20260425_1104                   # restore a backup (timestamp prefix match ok)
ocs restore <ts> --profile "Name"           # restore a single profile from a backup
ocs prune-backups --keep 20                 # preview deletion of old timestamped backups
                                            #   (--execute + typed "yes"; curated dirs never touched)
```

Global flags `--profile-dir` / `--system-profiles` override the default paths;
`clean`, `fix`, `remove-printer`, and `backup` accept `--backup-dir` (an
additional mirror; the default `_backup` is always written); `restore --force`
skips confirmation. `restore` has no `--backup-dir` — it always reads `_backup`.

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
- EXCEPTION — toolchangers: a printer whose tools each run a different nozzle is
  ONE machine named with NO nozzle suffix (e.g. `Snapmaker U1`); the per-tool
  nozzle lives in the filament/process names (`U1 Hotend - 0.4mm`, `U1 - 0.5mm`).
  Do not append a nozzle to such a machine or split it per-nozzle. Link matching
  handles the mismatch: `_machine_matches_hardware` resolves a hardware chunk's
  identifier WORD through `config.model_aliases` (`u1 -> snapmaker u1`), so a
  role-noun like "Hotend" no longer blocks the match and any per-tool nozzle
  resolves to the single machine.

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

- Hardware aliases (`config.hardware_aliases`, default `{"mako": "bambu",
  "tk": "teakettle"}`): maps abbreviations in profile names to machine name terms
  for link matching (e.g., "mako" -> "bambu").
- Model aliases (`config.model_aliases`, default includes `"u1": "snapmaker u1"`):
  ALSO consulted by `_machine_matches_hardware` at the word level, so a plain
  model name matches a hardware chunk that carries a role noun or nozzle (see the
  toolchanger exception in Domain Model).
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
