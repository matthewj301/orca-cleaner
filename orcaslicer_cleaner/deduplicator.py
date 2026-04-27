"""Duplicate detection for OrcaSlicer profiles."""

from __future__ import annotations

import hashlib
import json
import re
from itertools import combinations

from rapidfuzz import fuzz

from .models import DuplicateGroup, Profile, ProfileCategory

# Thresholds
CONTENT_SIMILARITY_THRESHOLD = 0.95

# ---------------------------------------------------------------------------
# Variation suffix detection
# ---------------------------------------------------------------------------

# Common iteration/test suffixes users append when tweaking profiles.
# Matches things like: " - beta", " beta", " - v2", " V1", " - test",
# " - sept fix", " - b", "Beta2", " - sept", " copy", " (2)" etc.
_VARIATION_SUFFIX_RE = re.compile(
    r"(?:"
    r"\s*-\s*(?:beta\d*|test\d*|v\d+|b\d*|copy\d*|old|new|backup|tmp|temp|fix|orig)"
    r"|\s+(?:beta\d*|test\d*|v\d+|b\d*|copy\d*|old|new|backup|tmp|temp|fix|orig)"
    r"|\s*-\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:\s+fix)?"
    r"|\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:\s+fix)?"
    r"|\s*\(\d+\)"
    r"|\s*_\d+$"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_variation_suffix(name: str) -> str:
    """Repeatedly strip known variation suffixes from a name."""
    prev = None
    stripped = name
    while stripped != prev:
        prev = stripped
        stripped = _VARIATION_SUFFIX_RE.sub("", stripped).rstrip()
    return stripped


def _is_variation_of(name_a: str, name_b: str) -> bool:
    """Check whether two names are variations of the same base profile.

    Returns True if stripping iteration/test suffixes from either name
    produces a string that matches the other exactly.  Also catches
    cases where one name IS the base of the other (before suffix was
    stripped).

    Fuzzy matching is intentionally NOT used here because single-digit
    differences like "20K" vs "50K" or "85A" vs "95A" are semantically
    meaningful (different materials/grades), not typos.
    """
    base_a = _strip_variation_suffix(name_a)
    base_b = _strip_variation_suffix(name_b)

    # If the bases are identical after stripping, it's a variation.
    if base_a.lower() == base_b.lower():
        return True

    # Check if one name's base matches the other's full name
    # (handles "Foo" vs "Foo - beta" where only one has the suffix).
    if base_a.lower() == name_b.strip().lower():
        return True
    if base_b.lower() == name_a.strip().lower():
        return True

    return False


# ---------------------------------------------------------------------------
# Name parsing for filament and process profiles
# ---------------------------------------------------------------------------

# Filament: "Material - Brand (Hardware)"
# e.g. "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm)"
_FILAMENT_NAME_RE = re.compile(
    r"^(?P<material>[^-]+?)\s*-\s*(?P<brand>[^(]+?)\s*\((?P<hardware>.+)\)\s*(?P<suffix>.*)$"
)

# Filament without hardware parenthetical
_FILAMENT_NAME_NO_HW_RE = re.compile(
    r"^(?P<material>[^-]+?)\s*-\s*(?P<brand>.+)$"
)

# Process: "LayerHeight - Purpose (Hardware)"
# e.g. "0.20mm - Production (LGX Lite Pro - Chube Air - 0.5mm)"
_PROCESS_NAME_RE = re.compile(
    r"^(?P<layer_height>\d+\.?\d*mm)\s*-\s*(?P<purpose>[^(]+?)\s*\((?P<hardware>.+)\)\s*(?P<suffix>.*)$"
)

# Process without hardware parenthetical
_PROCESS_NAME_NO_HW_RE = re.compile(
    r"^(?P<layer_height>\d+\.?\d*mm)\s*-\s*(?P<purpose>.+)$"
)


def _parse_filament_name(name: str) -> tuple[str, str, str] | None:
    """Parse a filament name into (material, brand, hardware).

    Returns None if the name doesn't match the expected pattern.
    """
    m = _FILAMENT_NAME_RE.match(name)
    if m:
        return (
            m.group("material").strip().lower(),
            _strip_variation_suffix(m.group("brand").strip()).lower(),
            m.group("hardware").strip().lower(),
        )
    m = _FILAMENT_NAME_NO_HW_RE.match(name)
    if m:
        return (
            m.group("material").strip().lower(),
            _strip_variation_suffix(m.group("brand").strip()).lower(),
            "",
        )
    return None


def _parse_process_name(name: str) -> tuple[str, str, str] | None:
    """Parse a process name into (layer_height, purpose, hardware).

    Returns None if the name doesn't match the expected pattern.
    """
    m = _PROCESS_NAME_RE.match(name)
    if m:
        return (
            m.group("layer_height").strip().lower(),
            _strip_variation_suffix(m.group("purpose").strip()).lower(),
            m.group("hardware").strip().lower(),
        )
    m = _PROCESS_NAME_NO_HW_RE.match(name)
    if m:
        return (
            m.group("layer_height").strip().lower(),
            _strip_variation_suffix(m.group("purpose").strip()).lower(),
            "",
        )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_duplicates(
    profiles: dict[ProfileCategory, list[Profile]],
    name_threshold: float = 0,  # kept for CLI compat; unused now
) -> list[DuplicateGroup]:
    """Find duplicate profiles across all categories."""
    groups: list[DuplicateGroup] = []

    for category in ProfileCategory:
        category_profiles = profiles.get(category, [])
        if len(category_profiles) < 2:
            continue

        # Pass 1: exact content duplicates (by hash) -- always valid
        groups.extend(_find_exact_dupes(category_profiles))

        # Pass 2: variation-based name matching (domain-aware)
        groups.extend(_find_variation_dupes(category_profiles, category))

    # Deduplicate groups (a pair might appear in both passes)
    return _merge_groups(groups)


# ---------------------------------------------------------------------------
# Exact content dedup (unchanged)
# ---------------------------------------------------------------------------


def _content_hash(profile: Profile) -> str | None:
    """SHA-256 of normalized settings for exact-match comparison."""
    stripped = profile.settings_without_metadata()
    if not stripped:
        return None
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _find_exact_dupes(profiles: list[Profile]) -> list[DuplicateGroup]:
    """Find profiles with identical settings (ignoring metadata)."""
    hash_to_profiles: dict[str, list[Profile]] = {}

    for p in profiles:
        h = _content_hash(p)
        if h is None:
            continue
        hash_to_profiles.setdefault(h, []).append(p)

    groups: list[DuplicateGroup] = []
    for h, ps in hash_to_profiles.items():
        if len(ps) > 1:
            groups.append(
                DuplicateGroup(
                    profiles=ps,
                    similarity_score=1.0,
                    match_type="exact_content",
                    details=f"Identical settings (hash: {h[:12]}...)",
                )
            )

    return groups


# ---------------------------------------------------------------------------
# Domain-aware variation detection
# ---------------------------------------------------------------------------


def _find_variation_dupes(
    profiles: list[Profile],
    category: ProfileCategory,
) -> list[DuplicateGroup]:
    """Find profiles that are iteration variants of each other.

    Uses domain-aware parsing so that profiles with different hardware
    configs are never flagged -- only true variation suffixes (beta, test,
    v2, etc.) on otherwise matching profiles.
    """
    groups: list[DuplicateGroup] = []

    for a, b in combinations(profiles, 2):
        is_dup, details = _check_variation_pair(a, b, category)
        if not is_dup:
            continue

        # Check content similarity to annotate match quality
        content_sim = _content_similarity(a, b)
        match_type = (
            "content_similar"
            if content_sim > CONTENT_SIMILARITY_THRESHOLD
            else "name_similar"
        )

        groups.append(
            DuplicateGroup(
                profiles=[a, b],
                similarity_score=max(content_sim, 0.90),
                match_type=match_type,
                details=f"{details}, Content: {content_sim:.0%}",
            )
        )

    return groups


def _check_variation_pair(
    a: Profile, b: Profile, category: ProfileCategory
) -> tuple[bool, str]:
    """Check if two profiles are variations using domain-aware parsing.

    Returns (is_variation, explanation_string).
    """
    # First gate: the names must be variations of each other.
    if not _is_variation_of(a.name, b.name):
        return False, ""

    # Category-specific structural checks
    if category == ProfileCategory.FILAMENT:
        return _check_filament_pair(a.name, b.name)
    elif category == ProfileCategory.PROCESS:
        return _check_process_pair(a.name, b.name)
    else:
        # Machine profiles: just use variation check
        return True, "Name variation detected"


def _check_filament_pair(name_a: str, name_b: str) -> tuple[bool, str]:
    """Check a filament profile pair.

    Only flag if material AND brand AND hardware all match (modulo
    variation suffixes).  Different hardware = NOT a duplicate.
    """
    pa = _parse_filament_name(name_a)
    pb = _parse_filament_name(name_b)

    if pa is None or pb is None:
        # Can't parse -- fall back to pure variation check (already passed)
        return True, "Name variation (unparseable filament format)"

    a_mat, a_brand, a_hw = pa
    b_mat, b_brand, b_hw = pb

    # Material must match closely
    if fuzz.ratio(a_mat, b_mat) < 90:
        return False, ""

    # Brand must match closely
    if fuzz.ratio(a_brand, b_brand) < 90:
        return False, ""

    # Hardware must match exactly (different hardware = different profile)
    if a_hw != b_hw:
        # Allow very minor differences (typos)
        if fuzz.ratio(a_hw, b_hw) < 95:
            return False, ""

    return True, f"Filament variation: {a_mat}/{a_brand} ({a_hw})"


def _check_process_pair(name_a: str, name_b: str) -> tuple[bool, str]:
    """Check a process profile pair.

    Only flag if layer height AND hardware match, and the purpose
    portion has a variation suffix.
    """
    pa = _parse_process_name(name_a)
    pb = _parse_process_name(name_b)

    if pa is None or pb is None:
        return True, "Name variation (unparseable process format)"

    a_lh, a_purpose, a_hw = pa
    b_lh, b_purpose, b_hw = pb

    # Layer height must match exactly
    if a_lh != b_lh:
        return False, ""

    # Hardware must match exactly (different hardware = different profile)
    if a_hw != b_hw:
        if fuzz.ratio(a_hw, b_hw) < 95:
            return False, ""

    # Purpose must match (after stripping variation suffixes)
    if fuzz.ratio(a_purpose, b_purpose) < 90:
        return False, ""

    return True, f"Process variation: {a_lh}/{a_purpose} ({a_hw})"


# ---------------------------------------------------------------------------
# Content similarity (unchanged)
# ---------------------------------------------------------------------------


def _content_similarity(a: Profile, b: Profile) -> float:
    """Compute what fraction of settings keys have identical values."""
    sa = a.settings_without_metadata()
    sb = b.settings_without_metadata()

    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0

    all_keys = set(sa) | set(sb)
    if not all_keys:
        return 1.0

    matching = sum(1 for k in all_keys if sa.get(k) == sb.get(k))
    return matching / len(all_keys)


# ---------------------------------------------------------------------------
# Group merging (unchanged)
# ---------------------------------------------------------------------------


def _merge_groups(groups: list[DuplicateGroup]) -> list[DuplicateGroup]:
    """Merge overlapping duplicate groups.

    If profile A appears in group1 and group2, merge those groups into one.
    Prefer the highest similarity score and best match_type.
    """
    if not groups:
        return []

    # Build adjacency: map each profile name to the groups it belongs to
    profile_to_groups: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        for p in g.profiles:
            key = f"{p.category.value}:{p.name}"
            profile_to_groups.setdefault(key, []).append(i)

    # Union-find to merge overlapping groups
    parent = list(range(len(groups)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for indices in profile_to_groups.values():
        for i in range(1, len(indices)):
            union(indices[0], indices[i])

    # Collect merged groups
    merged: dict[int, DuplicateGroup] = {}
    for i, g in enumerate(groups):
        root = find(i)
        if root not in merged:
            merged[root] = DuplicateGroup(
                profiles=[],
                similarity_score=g.similarity_score,
                match_type=g.match_type,
                details=g.details,
            )
        existing = merged[root]
        # Add profiles not already in the group
        existing_names = {f"{p.category.value}:{p.name}" for p in existing.profiles}
        for p in g.profiles:
            key = f"{p.category.value}:{p.name}"
            if key not in existing_names:
                existing.profiles.append(p)
                existing_names.add(key)
        # Keep highest similarity and best match type
        if g.similarity_score > existing.similarity_score:
            existing.similarity_score = g.similarity_score
        type_priority = {"exact_content": 3, "content_similar": 2, "name_similar": 1}
        if type_priority.get(g.match_type, 0) > type_priority.get(existing.match_type, 0):
            existing.match_type = g.match_type
            existing.details = g.details

    return list(merged.values())
