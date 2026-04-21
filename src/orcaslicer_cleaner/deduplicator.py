"""Duplicate detection for OrcaSlicer profiles."""

from __future__ import annotations

import hashlib
import json
import re
from itertools import combinations

from rapidfuzz import fuzz

from .models import DuplicateGroup, Profile, ProfileCategory

# Thresholds
NAME_SIMILARITY_THRESHOLD = 88
CONTENT_SIMILARITY_THRESHOLD = 0.95

# Pattern: "Material - Brand (Hardware - Config - Nozzle)"
# e.g. "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm)"
_PROFILE_NAME_RE = re.compile(
    r"^(?P<material>[^-]+?)\s*-\s*(?P<brand>[^(]+?)\s*(?:\((?P<hardware>.+)\))?$"
)


def parse_profile_name(name: str) -> tuple[str, str, str]:
    """Parse a profile name into (material, brand, hardware) components.

    Returns normalized lowercase components, or the full name as material
    if the pattern doesn't match.
    """
    m = _PROFILE_NAME_RE.match(name)
    if m:
        material = m.group("material").strip().lower()
        brand = m.group("brand").strip().lower()
        hardware = (m.group("hardware") or "").strip().lower()
        return material, brand, hardware
    return name.strip().lower(), "", ""


def find_duplicates(
    profiles: dict[ProfileCategory, list[Profile]],
    name_threshold: float = NAME_SIMILARITY_THRESHOLD,
) -> list[DuplicateGroup]:
    """Find duplicate profiles across all categories."""
    groups: list[DuplicateGroup] = []

    for category in ProfileCategory:
        category_profiles = profiles.get(category, [])
        if len(category_profiles) < 2:
            continue

        # Pass 1: exact content duplicates (by hash)
        groups.extend(_find_exact_dupes(category_profiles))

        # Pass 2: name-similar profiles (structured matching)
        groups.extend(_find_name_similar(category_profiles, name_threshold))

    # Deduplicate groups (a pair might appear in both passes)
    return _merge_groups(groups)


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


def _find_name_similar(
    profiles: list[Profile], threshold: float
) -> list[DuplicateGroup]:
    """Find profiles with similar names using structured matching.

    Profiles are parsed into (material, brand, hardware) components.
    The material must match closely for the pair to be considered,
    preventing false positives like "ABS - Generic" vs "ASA - Generic".
    """
    groups: list[DuplicateGroup] = []
    parsed = [(p, parse_profile_name(p.name)) for p in profiles]

    for (a, (a_mat, a_brand, a_hw)), (b, (b_mat, b_brand, b_hw)) in combinations(parsed, 2):
        # Material must be very similar (catches typos like "Fillamentum" vs "Filamentum"
        # but rejects "ABS" vs "ASA")
        mat_score = fuzz.ratio(a_mat, b_mat)
        if mat_score < 80:
            continue

        # Compute overall structured similarity:
        # - Material similarity (weight 40%)
        # - Brand similarity (weight 35%)
        # - Hardware similarity (weight 25%)
        brand_score = fuzz.token_sort_ratio(a_brand, b_brand) if (a_brand and b_brand) else (100.0 if a_brand == b_brand else 0.0)
        hw_score = fuzz.token_sort_ratio(a_hw, b_hw) if (a_hw and b_hw) else (100.0 if a_hw == b_hw else 0.0)

        weighted_score = mat_score * 0.40 + brand_score * 0.35 + hw_score * 0.25

        if weighted_score >= threshold:
            content_sim = _content_similarity(a, b)
            groups.append(
                DuplicateGroup(
                    profiles=[a, b],
                    similarity_score=weighted_score / 100.0,
                    match_type="content_similar" if content_sim > CONTENT_SIMILARITY_THRESHOLD else "name_similar",
                    details=(
                        f"Material: {mat_score:.0f}%, Brand: {brand_score:.0f}%, "
                        f"Hardware: {hw_score:.0f}% (weighted: {weighted_score:.0f}%), "
                        f"Content: {content_sim:.0%}"
                    ),
                )
            )

    return groups


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
