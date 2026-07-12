"""Tests for the configurable naming-grammar engine (naming.py).

Two things are guarded here:
  1. The DEFAULT grammar parses names identically to the original hardcoded
     regexes (kept below as a reference implementation) — including their
     quirks. This is the regression net for the deduplicator/matrix refactor.
  2. A CUSTOM format template actually changes parsing, proving the grammar is
     configurable (the whole point of this phase).
"""

from __future__ import annotations

import re

import pytest

from orcaslicer_cleaner.config import DEFAULT_CONFIG, load_config
from orcaslicer_cleaner.deduplicator import _parse_filament_name, _parse_process_name
from orcaslicer_cleaner.naming import compile_grammar

# --- Reference implementation: the ORIGINAL regexes, verbatim ---------------
# If the engine ever diverges from these on the corpus below, the parity test
# fails. This freezes the pre-refactor behavior independently of naming.py.
_REF_FIL = re.compile(r"^(?P<material>[^-]+?)\s*-\s*(?P<brand>[^(]+?)\s*\((?P<hardware>.+)\)\s*(?P<suffix>.*)$")
_REF_FIL_NOHW = re.compile(r"^(?P<material>[^-]+?)\s*-\s*(?P<brand>.+)$")
_REF_PROC = re.compile(r"^(?P<layer_height>\d+\.?\d*mm)\s*-\s*(?P<purpose>[^(]+?)\s*\((?P<hardware>.+)\)\s*(?P<suffix>.*)$")
_REF_PROC_NOHW = re.compile(r"^(?P<layer_height>\d+\.?\d*mm)\s*-\s*(?P<purpose>.+)$")


def _ref_filament(name):
    m = _REF_FIL.match(name) or _REF_FIL_NOHW.match(name)
    if not m:
        return None
    gd = m.groupdict()
    return (gd["material"].strip().lower(), gd["brand"].strip().lower(), gd.get("hardware", "").strip().lower() if gd.get("hardware") else "")


def _ref_process(name):
    m = _REF_PROC.match(name) or _REF_PROC_NOHW.match(name)
    if not m:
        return None
    gd = m.groupdict()
    return (gd["layer_height"].strip().lower(), gd["purpose"].strip().lower(), gd.get("hardware", "").strip().lower() if gd.get("hardware") else "")


# A corpus spanning the tricky forms found in the real library + tests.
CORPUS = [
    "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm)",
    "ABS - Filamentum (LGX Lite Pro - TK - 0.4mm) - beta",
    "ABS (2.0) - Fusion Filaments (WWBMG - TeaKettle - 0.4mm)",   # (2.0) stays in material
    "PLA - Shared",
    "PLA - Filamentum (Doomcube)",
    "PolyMaker - PolyTerra PLA (Mako) - MM",
    "Generic PLA",
    "Just A Name",
    "ASA -3DO",
    "ASA- 3DO",
    "ASA-CF - 3DO",
    "PLA-CF Profile",
    "0.20mm - Production (LGX Lite Pro - Chube Air - 0.5mm)",
    "0.2mm - Draft (Doomcube - 0.40mm)",
    "0.20mm - Standard",
    "0.20mm - PLA+ (Satin) - Production (X1C - 0.4mm)",           # greedy-to-last-paren quirk
    "0.20mm - BB3D Production - PolyTerra PLA - (Doomcube - 0.4mm)",
    "0.08mm - HQ",
    "Orphan Process - Standard",
    "RatRig V-Core 3.1",
]


def _engine_raw(category_naming, name):
    """Raw grammar output as a (f0, f1, hardware) lowercased tuple, or None —
    directly comparable to the reference regexes (no suffix stripping)."""
    fields = compile_grammar(category_naming.format).parse(name)
    if fields is None:
        return None
    order = re.findall(r"\{(\w+)\}", category_naming.format)
    vals = [fields.get(k, "").lower() for k in order]
    hw = fields.get("hardware", "").lower()
    return (vals[0], vals[1], hw)


class TestParityWithOriginalRegexes:
    @pytest.mark.parametrize("name", CORPUS)
    def test_filament_matches_reference(self, name):
        assert _engine_raw(DEFAULT_CONFIG.naming.filament, name) == _ref_filament(name)

    @pytest.mark.parametrize("name", CORPUS)
    def test_process_matches_reference(self, name):
        assert _engine_raw(DEFAULT_CONFIG.naming.process, name) == _ref_process(name)


class TestDefaultGrammarExactOutput:
    def test_filament_full(self):
        assert _parse_filament_name("ABS - Filamentum (LGX Lite Pro - TK - 0.4mm)") == (
            "abs", "filamentum", "lgx lite pro - tk - 0.4mm",
        )

    def test_filament_paren_in_material_quirk(self):
        assert _parse_filament_name("ABS (2.0) - Fusion Filaments (WWBMG - TeaKettle - 0.4mm)") == (
            "abs (2.0)", "fusion filaments", "wwbmg - teakettle - 0.4mm",
        )

    def test_filament_suffix_ignored_and_brand_stripped(self):
        assert _parse_filament_name("ABS - Filamentum (LGX Lite Pro - TK - 0.4mm) - beta") == (
            "abs", "filamentum", "lgx lite pro - tk - 0.4mm",
        )

    def test_filament_no_hardware(self):
        assert _parse_filament_name("PLA - Shared") == ("pla", "shared", "")

    def test_filament_unparseable_returns_none(self):
        assert _parse_filament_name("Generic PLA") is None
        assert _parse_filament_name("Just A Name") is None

    def test_process_full(self):
        assert _parse_process_name("0.20mm - Production (LGX Lite Pro - Chube Air - 0.5mm)") == (
            "0.20mm", "production", "lgx lite pro - chube air - 0.5mm",
        )

    def test_process_greedy_to_last_paren_quirk(self):
        assert _parse_process_name("0.20mm - PLA+ (Satin) - Production (X1C - 0.4mm)") == (
            "0.20mm", "pla+", "satin) - production (x1c - 0.4mm",
        )

    def test_process_requires_layer_height(self):
        # First field must look like a dimension, else None.
        assert _parse_process_name("ASA-CF - 3DO") is None
        assert _parse_process_name("Orphan Process - Standard") is None
        assert _parse_process_name("PLA-CF Profile") is None

    def test_process_no_hardware(self):
        assert _parse_process_name("0.20mm - Standard") == ("0.20mm", "standard", "")


class TestCustomGrammar:
    def test_custom_filament_format_changes_parsing(self, tmp_path):
        # A convention with brand first and square-bracket hardware.
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[naming.filament]\nformat = "{brand} {material} [{hardware}]"\n',
            encoding="utf-8",
        )
        cfg = load_config(cfg_file)
        # Default grammar cannot parse this shape:
        assert _parse_filament_name("Filamentum ABS [x1c]") is None
        # Custom grammar can:
        assert _parse_filament_name("Filamentum ABS [x1c]", cfg) == ("abs", "filamentum", "x1c")

    def test_custom_grammar_is_cached(self):
        a = compile_grammar(DEFAULT_CONFIG.naming.filament.format)
        b = compile_grammar(DEFAULT_CONFIG.naming.filament.format)
        assert a is b  # lru_cache returns the same compiled object

    def test_grammar_exposes_field_order(self):
        g = compile_grammar("{material} - {brand} ({hardware})")
        assert g.fields == ("material", "brand", "hardware")

    def test_duplicate_field_name_rejected(self):
        from orcaslicer_cleaner.naming import GrammarError

        with pytest.raises(GrammarError, match="repeats field"):
            compile_grammar("{material} - {material}")

    def test_format_without_fields_rejected(self):
        from orcaslicer_cleaner.naming import GrammarError

        with pytest.raises(GrammarError, match="no .*placeholders"):
            compile_grammar("just literal text")

    def test_hardware_not_last_no_fallback_truncation(self):
        # Regression: a mid-template {hardware} must NOT make the no-hardware
        # fallback silently drop the fields that follow it. With hardware
        # present the name parses; without it, the name simply returns None
        # (safe) rather than a corrupted partial parse.
        g = compile_grammar("{material} {hardware} - {brand}")
        assert g.fallback is None  # no lossy fallback generated
        assert g.parse("ABS x1c - Filamentum") == {
            "material": "ABS", "hardware": "x1c", "brand": "Filamentum",
        }
        # A name lacking the hardware token doesn't falsely parse into garbage.
        assert g.parse("ABS - Filamentum") is None

    def test_trailing_hardware_keeps_fallback(self):
        g = compile_grammar("{material} - {brand} ({hardware})")
        assert g.fallback is not None
        assert g.parse("PLA - Shared") == {"material": "PLA", "brand": "Shared"}

    def test_bad_format_surfaces_as_config_error(self, tmp_path):
        from orcaslicer_cleaner.config import ConfigError

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[naming.filament]\nformat = "{material} - {material}"\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match=r"\[naming.filament\].*repeats field"):
            load_config(cfg_file)


class TestRenderableValidation:
    """A {hardware} field must be wrapped in a single-char bracket so the
    standardizer can build AND re-detect it (else names corrupt/regrow)."""

    def test_hardware_without_bracket_rejected(self):
        from orcaslicer_cleaner.naming import GrammarError, validate_renderable

        with pytest.raises(GrammarError, match="single-character bracket"):
            validate_renderable("{brand}{hardware}")

    def test_multichar_bracket_rejected(self):
        from orcaslicer_cleaner.naming import GrammarError, validate_renderable

        with pytest.raises(GrammarError, match="single-character bracket"):
            validate_renderable("{material} - {brand} <<{hardware}>>")

    def test_single_char_brackets_ok(self):
        from orcaslicer_cleaner.naming import validate_renderable

        validate_renderable("{material} - {brand} ({hardware})")
        validate_renderable("{material} - {brand} [{hardware}]")
        validate_renderable("{model} - {nozzle}")  # no hardware field: fine

    def test_unrenderable_hardware_surfaces_as_config_error(self, tmp_path):
        from orcaslicer_cleaner.config import ConfigError

        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            '[naming.filament]\nformat = "{brand}{hardware}"\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match=r"\[naming.filament\].*single-character bracket"):
            load_config(cfg_file)

    def test_has_hardware_false_without_bracket(self):
        from orcaslicer_cleaner.naming import render_spec

        assert render_spec("{brand}{hardware}").has_hardware is False
        assert render_spec("{material} - {brand} ({hardware})").has_hardware is True
