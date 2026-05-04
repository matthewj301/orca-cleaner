"""Tests for name standardization rules.

Regression test: the layer-height padding rule only applies to mm values at
the START of a name (process profile layer heights like "0.2mm - Production").
It must NOT pad nozzle sizes which appear at the end of names or in parentheses.
"""

import pytest

from orcaslicer_cleaner.standardizer import _normalize_name


class TestLayerHeightPadding:
    """Rule 1: pad single-digit layer heights at start of name to two decimal places."""

    @pytest.mark.parametrize("input_name,expected", [
        ("0.2mm - Production", "0.20mm - Production"),
        ("0.3mm", "0.30mm"),
        ("0.1mm Fine", "0.10mm Fine"),
        ("0.6mm - Draft", "0.60mm - Draft"),
    ])
    def test_pads_layer_heights_at_start(self, input_name, expected):
        assert _normalize_name(input_name) == expected

    @pytest.mark.parametrize("input_name", [
        "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
        "Bambu Lab X1 Carbon - 0.4mm",
        "RatRig V-Core 3.1 - LGX Pro Metal - Chube Conduction - 0.5mm",
        "Positron - Sherpa Micro - 0.6mm",
        "Some Printer - 0.8mm",
        "ASA - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)",
        "PETG - 3DO (LGX Lite Pro, TeaKettle 0.4mm)",
        "ABS - Fillamentum (0.4mm)",
        "Snapmaker U1 - 0.40mm",
    ])
    def test_does_not_pad_nozzle_sizes(self, input_name):
        assert _normalize_name(input_name) == input_name

    def test_already_padded_unchanged(self):
        assert _normalize_name("0.20mm - Production") == "0.20mm - Production"
        assert _normalize_name("0.08mm - HQ") == "0.08mm - HQ"


class TestSpacedHyphens:
    """Rule 2: normalize spaced hyphens to ' - '."""

    def test_normalizes_spaced_hyphens(self):
        assert _normalize_name("ASA -3DO") == "ASA - 3DO"
        assert _normalize_name("ASA- 3DO") == "ASA - 3DO"
        assert _normalize_name("ASA  -  3DO") == "ASA - 3DO"

    def test_preserves_compound_words(self):
        assert "V-Core" in _normalize_name("RatRig V-Core 3.1")
        assert "ASA-CF" in _normalize_name("ASA-CF - 3DO")
        assert "PLA-CF" in _normalize_name("PLA-CF Profile")
