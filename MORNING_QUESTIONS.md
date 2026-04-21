# Morning Review - OrcaSlicer Profile Cleaner

The tool is built, installed, and working against your real profiles. Three
independent review agents audited the code for bugs, features, UX, and safety.
All bugs they found have been fixed. Here's what I need your input on.

## Status

The CLI (`ocs`) is fully functional with four commands:
- `ocs scan` — full report (validation + deduplication)
- `ocs validate` — validation only
- `ocs dedupe` — duplicate detection only
- `ocs clean` — dry-run cleanup (use `--execute` to apply)

Run `ocs scan` to see everything, or `ocs scan --json-output` for machine-readable output.

## Scan Results Against Your Profiles

| Metric | Count |
|--------|-------|
| Total profiles loaded | 304 (103 filament, 8 machine, 193 process) |
| Errors | 0 |
| Warnings | 110 |
| Info items | 113 |
| Duplicate groups | 43 |

### Key Findings

**1. 104 broken printer references** — profiles pointing to machines that no longer exist:
- `RatRig V-Core 3.1 - VZ-Plus - Goliath - 0.4mm` — 26 profiles reference this
- `RatRig V-Core 3.1 - VZ-Plus - Goliath - 0.6mm` — 16 profiles
- `Doomcube - LGX Lite Pro - Chube Air - 0.5mm` — 15 profiles
- `RatRig V-Core 3.1 - VZ-Plus - Chube Conduction - 0.6mm` — 13 profiles
- `Annex K3 - Sherpa Mini - TeaKettle - 0.5mm` — 7 profiles
- Plus 6 more old machine configs (MyKlipper, Voron 2.4, old Snapmaker, old Voron 0.1, etc.)

**2. 6 orphaned .info files** (no matching .json) — all for Sherpa Mini 10t / Rapido 2 UHF configs.

**3. 113 stale profiles** (not updated in 365+ days).

**4. 5 content-similar groups** where settings are >95% identical:
- 33 Production/Whistles/Coins process variants that are nearly the same
- Whistles profiles at different layer heights (0.22, 0.24, 0.28mm) with near-identical settings
- K3 Whistles and beta variants
- Nozzle 0.2 profiles at different layer heights

**5. 38 name-similar groups** — profiles with similar names but different settings.

---

## Questions For You

### Priority 1: Core Behavior

**Q1: Broken references — fix or flag?**
Should the tool offer to update `compatible_printers` references when a machine
was renamed (e.g., map `Doomcube - LGX Lite Pro - Chube Air - 0.5mm` to a current
machine name)? Or just flag them?
I could build an interactive "remap this old printer to this new one" flow.

**Q2: What's your stale threshold?**
With 365 days, 113 profiles are flagged as stale. With 730 days (2 years), only
the orphaned files show up. What's a useful cutoff for you? Or should stale
detection be based on something else (e.g., references a machine you no longer have)?

**Q3: The "default" vs numbered user directory**
Your `default/` dir has just one machine profile (Voron 0.2 - 0.4mm). Should the
tool treat these directories differently? Should it ignore `default/`?

**Q4: Cloud sync**
Do you use OrcaSlicer's cloud sync? If yes, archiving profiles locally might get
them re-downloaded on next sync. The tool should warn about this or we'd need to
handle it differently.

### Priority 2: Deduplication

**Q5: What constitutes a "duplicate" in your workflow?**
I see patterns like:
- `0.20mm - PLA+ - Production - Coins` / `Coins - Beta` / `Coins - Beta2` / `Coins - sept` / `Coins - sept fix`
- `0.24mm - Whistles beta` / `0.24mm - Whistles - Beta`

Are these intentional iterations you want to keep? Or would you like the tool to
identify "beta/test" variants and recommend consolidating to the latest?

**Q6: Hardware-specific variants**
Many filament profiles exist per-hardware config (e.g., 4 "ABS - Filamentum"
profiles for different extruder/hotend combos). These are flagged as name-similar
but are intentionally different. Should the tool:
(a) Ignore profiles that differ only by hardware config? (They're expected)
(b) Still show them but in a separate "FYI" section?
(c) Current behavior is fine?

**Q7: Process profile grouping is noisy**
Process profiles don't follow the `Material - Brand (Hardware)` naming pattern.
They follow `0.20mm - Purpose (Hardware)` which makes fuzzy matching group a lot
of intentionally different profiles. Should I add special handling for process
profiles, or is the `--name-threshold 95` flag sufficient?

### Priority 3: Features (Reviewer Recommendations, ranked by impact)

**Q8: System profile awareness** (all 3 reviewers flagged this)
The `inherits` field in your profiles references system presets like
`"0.20mm Standard @Voron"` and `"Voron 2.4 300 0.4 nozzle"`. These live in
OrcaSlicer's app bundle, not your user directory. Currently the tool can't
validate these references. Should I add read-only loading of system profiles
from `/Applications/OrcaSlicer.app/Contents/Resources/profiles/`? This would:
- Validate `inherits` chains (a broken inherit silently falls back to defaults)
- Reduce false positives in `compatible_printers` checks (some references
  may be to system-bundled machines, not user-defined ones)

**Q9: `ocs diff` command**
For content-similar groups, show exactly which settings differ between two
profiles side-by-side. Would this be useful for deciding which beta to keep?

**Q10: `ocs restore` command**
Add a way to undo cleanup operations from backups (now timestamped per-run).
Right now restoring requires manual file moves.

**Q11: Batch operations for broken references**
Beyond the current cleanup (archive orphans + exact dupes), would you want:
- "Archive all profiles referencing machine X"
- "Remap all references from old machine name to new machine name"
- "Delete all profiles older than N days that reference non-existent machines"

**Q12: Filament type validation**
OrcaSlicer uses a `filament_type` field for plate temperature matching. I could
add an allowlist check to catch typos (e.g., `"ASB"` instead of `"ABS"`). Worth doing?

**Q13: Naming convention consistency**
Your process profiles mix `0.2mm` and `0.20mm` as prefixes. I could flag profiles
that don't match the majority convention. Helpful or just noise?

---

## Bugs Found & Fixed By Reviewers

All bugs identified by the three review agents have been fixed:

1. **`--dry-run` flag was broken** — always True, never read. Removed in favor of
   `--execute` as the opt-in flag (dry-run is now the default behavior).

2. **Backup had no timestamp** — running `clean --execute` twice would overwrite
   the previous backup. Now creates timestamped subdirs (`_backup/20260419_053000/`).

3. **Backup collision** — two profiles with the same name from different source dirs
   could overwrite each other. Now appends a counter on collision.

4. **Enum compared by string** — `issue.issue_type.value == "orphaned_file"` replaced
   with proper enum comparison `issue.issue_type == IssueType.ORPHANED_FILE`.

5. **`import datetime` inside loop** — moved to module top-level.

6. **Duplicate setting_id only reported second occurrence** — now reports all profiles
   sharing a duplicate ID.

7. **`printer_settings_id` not stripped in content comparison** — added to the skip set
   so machine profiles with different names but identical settings are properly detected.

8. **`scan` command always exited 0** — now exits 1 on errors (usable in CI).

9. **No severity filter** — added `--min-severity` flag to `scan` command to reduce noise.

10. **Fuzzy matching was too aggressive** — "ABS - Generic" matched "ASA - Generic" at 95%.
    Fixed with structured name parsing: material (40%), brand (35%), hardware (25%).

11. **JSON output mixed with status messages** — "Loaded N profiles..." now goes to stderr.

## To Try

```bash
ocs scan                              # full report
ocs scan --min-severity warning       # skip INFO (stale profile notices)
ocs scan --json-output                # machine-readable
ocs validate --stale-days 730         # only flag very old profiles
ocs dedupe --name-threshold 95        # stricter matching (20 groups vs 43)
ocs clean                             # preview safe cleanup actions
ocs clean --execute                   # actually do it (DON'T RUN YET)
```
