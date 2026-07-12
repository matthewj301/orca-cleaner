"""User configuration: naming grammar, vocabulary, and tunable thresholds.

Everything the tool assumes about *your* profile library — the name formats,
the hardware nicknames, the fuzzy-match cutoffs — lives here as data with
built-in defaults. A user can override any of it via a TOML file (default
``~/.config/orcaslicer-cleaner/config.toml``, or ``--config PATH``). Omitted
keys fall back to the defaults, so a partial config only changes what it names.

The defaults reproduce the tool's original hardcoded behavior exactly, so the
existing test suite doubles as a regression net: default config == old
constants. Vocabulary sections (abbreviations, hardware_aliases, model_aliases)
REPLACE their default wholesale when present — every default entry is
author-specific, so a new user's section fully defines their own vocabulary
rather than inheriting someone else's hardware nicknames. Scalar sections
(thresholds, naming) merge per-key over the defaults.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or has unknown keys."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """Tunable numeric cutoffs. All had hardcoded values before config existed;
    the defaults here are those exact values."""

    stale_days: int = 365            # validators.py: not updated in N days = stale
    content_similarity: float = 0.95  # deduplicator.py: content_similar vs name_similar
    fuzz_material: int = 90          # deduplicator.py: filament material match
    fuzz_brand: int = 90             # deduplicator.py: filament brand match
    fuzz_hardware: int = 95          # deduplicator.py: hardware near-match (typos)
    fuzz_process_purpose: int = 90   # deduplicator.py: process purpose match
    fuzz_process_hardware: int = 95  # deduplicator.py: process hardware near-match
    diff_match_cutoff: int = 50      # cli.py: "did you mean?" fuzzy cutoff in diff
    blast_category_pct: float = 0.15  # safety.py: warn if archiving >N% of a category
    blast_category_floor: int = 3    # safety.py: min profiles before the % check bites
    blast_bulk: int = 20             # safety.py: warn if archiving >=N profiles at once
    unassigned_group_threshold: int = 15  # cli.py: group unassigned profiles above N


@dataclass(frozen=True)
class CategoryNaming:
    """Naming grammar for one profile category.

    ``format`` is a template of literal text and ``{field}`` placeholders, e.g.
    ``"{material} - {brand} ({hardware})"``. ``hardware`` is an optional
    sub-template expanding the ``{hardware}`` placeholder, e.g.
    ``"{extruder} - {hotend} - {nozzle}"``. Consumed by the Phase-2 naming
    engine; stored verbatim here so the config schema is stable.
    """

    format: str
    hardware: str | None = None


@dataclass(frozen=True)
class NamingConfig:
    filament: CategoryNaming = CategoryNaming(
        "{material} - {brand} ({hardware})", "{extruder} - {hotend} - {nozzle}"
    )
    process: CategoryNaming = CategoryNaming(
        "{layer} - {purpose} ({hardware})", "{model} - {nozzle}"
    )
    machine: CategoryNaming = CategoryNaming(
        "{model} - {extruder} - {hotend} - {nozzle}", None
    )
    # Formatting rules applied during standardization.
    pad_layer_heights: bool = True   # "0.2mm" -> "0.20mm" at name start
    trim_nozzle_zeros: bool = True   # "0.40mm" -> "0.4mm" in nozzle position
    normalize_hyphens: bool = True   # spaced hyphens -> " - "


def _default_abbreviations() -> dict[str, str]:
    return {"TK": "TeaKettle"}


def _default_hardware_aliases() -> dict[str, str]:
    return {"mako": "bambu", "tk": "teakettle"}


def _default_model_aliases() -> dict[str, str]:
    return {"bbl": "bambu lab", "x1c": "x1 carbon", "p1s": "p1s", "u1": "snapmaker u1"}


@dataclass(frozen=True)
class Config:
    # Vocabulary — expanded/aliased during naming and link matching. Every
    # default entry is the author's personal hardware; a user's config section
    # replaces the whole map (see module docstring).
    abbreviations: dict[str, str] = field(default_factory=_default_abbreviations)
    hardware_aliases: dict[str, str] = field(default_factory=_default_hardware_aliases)
    model_aliases: dict[str, str] = field(default_factory=_default_model_aliases)
    thresholds: Thresholds = field(default_factory=Thresholds)
    naming: NamingConfig = field(default_factory=NamingConfig)


DEFAULT_CONFIG = Config()

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "orcaslicer-cleaner" / "config.toml"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _known(dc_type) -> set[str]:
    return {f.name for f in fields(dc_type)}


def _reject_unknown(section: str, raw: dict, dc_type) -> None:
    unknown = set(raw) - _known(dc_type)
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_known(dc_type)))}."
        )


def _str_map(section: str, raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ConfigError(f"[{section}] must be a table of string = string entries.")
    return dict(raw)


def _category_naming(section: str, raw: object, default: CategoryNaming) -> CategoryNaming:
    if not isinstance(raw, dict):
        raise ConfigError(f"[naming.{section}] must be a table.")
    _reject_unknown(f"naming.{section}", raw, CategoryNaming)
    fmt = raw.get("format", default.format)
    hw = raw.get("hardware", default.hardware)
    if not isinstance(fmt, str) or (hw is not None and not isinstance(hw, str)):
        raise ConfigError(f"[naming.{section}] format/hardware must be strings.")
    return CategoryNaming(format=fmt, hardware=hw)


def _merge(defaults: Config, raw: dict) -> Config:
    """Merge a parsed TOML dict over the default config (see module docstring
    for per-section semantics). Raises ConfigError on unknown/mistyped keys."""
    _reject_unknown("(top level)", raw, Config)

    abbreviations = (
        _str_map("abbreviations", raw["abbreviations"])
        if "abbreviations" in raw
        else dict(defaults.abbreviations)
    )
    hardware_aliases = (
        _str_map("hardware_aliases", raw["hardware_aliases"])
        if "hardware_aliases" in raw
        else dict(defaults.hardware_aliases)
    )
    model_aliases = (
        _str_map("model_aliases", raw["model_aliases"])
        if "model_aliases" in raw
        else dict(defaults.model_aliases)
    )

    thresholds = defaults.thresholds
    if "thresholds" in raw:
        t = raw["thresholds"]
        if not isinstance(t, dict):
            raise ConfigError("[thresholds] must be a table.")
        _reject_unknown("thresholds", t, Thresholds)
        thresholds = replace(defaults.thresholds, **t)

    naming = defaults.naming
    if "naming" in raw:
        n = raw["naming"]
        if not isinstance(n, dict):
            raise ConfigError("[naming] must be a table.")
        _reject_unknown("naming", n, NamingConfig)
        naming = replace(
            defaults.naming,
            filament=_category_naming("filament", n["filament"], defaults.naming.filament)
            if "filament" in n
            else defaults.naming.filament,
            process=_category_naming("process", n["process"], defaults.naming.process)
            if "process" in n
            else defaults.naming.process,
            machine=_category_naming("machine", n["machine"], defaults.naming.machine)
            if "machine" in n
            else defaults.naming.machine,
            **{
                k: n[k]
                for k in ("pad_layer_heights", "trim_nozzle_zeros", "normalize_hyphens")
                if k in n
            },
        )

    # Fail fast on an unparseable format template — a clear config error up
    # front beats a raw regex crash mid-scan. Imported lazily to keep config
    # free of a hard dependency on the grammar engine.
    from .naming import GrammarError, compile_grammar, validate_renderable

    for cat_name, cat in (("filament", naming.filament), ("process", naming.process),
                          ("machine", naming.machine)):
        try:
            compile_grammar(cat.format)
            validate_renderable(cat.format)
        except GrammarError as e:
            raise ConfigError(f"[naming.{cat_name}] {e}") from e

    return Config(
        abbreviations=abbreviations,
        hardware_aliases=hardware_aliases,
        model_aliases=model_aliases,
        thresholds=thresholds,
        naming=naming,
    )


def load_config(path: Path | None = None) -> Config:
    """Load configuration, merging a TOML file over the built-in defaults.

    With no ``path``, looks for the default config file and silently returns
    ``DEFAULT_CONFIG`` if it isn't there. An explicit ``path`` that doesn't
    exist is an error (the user asked for a specific file). Malformed TOML,
    unknown keys, or mistyped values raise ``ConfigError``.
    """
    if path is None:
        if not DEFAULT_CONFIG_PATH.is_file():
            return DEFAULT_CONFIG
        path = DEFAULT_CONFIG_PATH
    else:
        path = Path(path)  # tolerate a str from non-CLI callers
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Malformed TOML in {path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Could not read config {path}: {e}") from e

    return _merge(DEFAULT_CONFIG, raw)
