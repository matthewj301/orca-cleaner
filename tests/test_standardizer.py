"""Tests for name standardization rules.

Regression test: the layer-height padding rule only applies to mm values at
the START of a name (process profile layer heights like "0.2mm - Production").
It must NOT pad nozzle sizes which appear at the end of names or in parentheses.
"""

import pytest

from orcaslicer_cleaner.standardizer import (
    _normalize_name,
    _normalize_process_paren,
    _extract_printer_model,
    _extract_nozzle_from_machine,
)
from orcaslicer_cleaner.models import Profile, ProfileCategory
from pathlib import Path


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


class TestProcessParenNormalization:
    """Process profiles use (PrinterModel - NozzleSize) format, not hardware details."""

    MACHINES = [
        "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
        "Doomcube - WWBMG - TeaKettle - 0.4mm",
        "RatRig V-Core 3.1 - LGX Pro Metal - Chube Conduction - 0.5mm",
        "Voron 0.1rc2 - Sherpa Mini 10t - TeaKettle - 0.4mm",
    ]

    @pytest.fixture
    def machine_models(self):
        return {m: _extract_printer_model(m) for m in self.MACHINES}

    @pytest.fixture
    def machines_by_model(self):
        result = {}
        for m in self.MACHINES:
            model = _extract_printer_model(m)
            result.setdefault(model, []).append(m)
        return result

    def _make_process(self, name, compatible_printers):
        return Profile(
            name=name,
            category=ProfileCategory.PROCESS,
            directory=Path("/tmp"),
            settings={"compatible_printers": compatible_printers},
        )

    def test_replaces_hardware_with_model(self, machine_models, machines_by_model):
        p = self._make_process(
            "0.20mm - Standard (LGX Lite Pro - TeaKettle - 0.4mm)",
            ["Doomcube - LGX Lite Pro - TeaKettle - 0.4mm"],
        )
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - Standard (Doomcube - 0.4mm)"

    def test_ratrig_hardware_replaced(self, machine_models, machines_by_model):
        p = self._make_process(
            "0.20mm - Draft (LGX Pro Metal - Chube - 0.50mm)",
            ["RatRig V-Core 3.1 - LGX Pro Metal - Chube Conduction - 0.5mm"],
        )
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - Draft (RatRig V-Core 3.1 - 0.5mm)"

    def test_voron_hardware_replaced(self, machine_models, machines_by_model):
        p = self._make_process(
            "0.20mm - PLA Metal (Sherpa Mini 10t - TeaKettle FIN - 0.4mm)",
            ["Voron 0.1rc2 - Sherpa Mini 10t - TeaKettle - 0.4mm"],
        )
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - PLA Metal (Voron 0.1rc2 - 0.4mm)"

    def test_already_correct_format_unchanged(self, machine_models, machines_by_model):
        p = self._make_process(
            "0.20mm - Standard (Doomcube - 0.4mm)",
            ["Doomcube - LGX Lite Pro - TeaKettle - 0.4mm"],
        )
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - Standard (Doomcube - 0.4mm)"

    def test_nozzle_only_paren_unchanged(self, machine_models, machines_by_model):
        p = self._make_process("0.20mm - Production (0.6mm)", [])
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - Production (0.6mm)"

    def test_empty_compatible_printers_unchanged(self, machine_models, machines_by_model):
        p = self._make_process(
            "0.20mm - Standard (Unknown HW - 0.5mm)", []
        )
        result = _normalize_process_paren(
            p.name, p, machine_models, machines_by_model
        )
        assert result == "0.20mm - Standard (Unknown HW - 0.5mm)"
