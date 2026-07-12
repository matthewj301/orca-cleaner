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
    ])
    def test_does_not_pad_nozzle_sizes(self, input_name):
        assert _normalize_name(input_name) == input_name

    def test_already_padded_unchanged(self):
        assert _normalize_name("0.20mm - Production") == "0.20mm - Production"
        assert _normalize_name("0.08mm - HQ") == "0.08mm - HQ"


class TestNozzleMinimalForm:
    """Rule 4: nozzle sizes (mm values NOT at the start of a name) drop
    trailing zeros — the mirror image of layer-height padding."""

    @pytest.mark.parametrize("input_name,expected", [
        ("Snapmaker U1 - 0.40mm", "Snapmaker U1 - 0.4mm"),
        ("HTPLA - Protopasta - U1 - 0.40mm", "HTPLA - Protopasta - U1 - 0.4mm"),
        ("ABS - 3DO (LGX Lite Pro - TeaKettle - 0.40mm)", "ABS - 3DO (LGX Lite Pro - TeaKettle - 0.4mm)"),
        ("0.20mm - Draft (RatRig V-Core 3.1 - 0.50mm)", "0.20mm - Draft (RatRig V-Core 3.1 - 0.5mm)"),
        ("Some Printer - 1.0mm", "Some Printer - 1mm"),
    ])
    def test_strips_trailing_zeros_from_nozzle_sizes(self, input_name, expected):
        assert _normalize_name(input_name) == expected

    @pytest.mark.parametrize("input_name", [
        "0.20mm - Production",           # leading layer height stays padded
        "0.40mm - K3 - Whistles",        # 0.40 layer height is valid, not a nozzle
        "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",  # already minimal
        # Regression (2026-07-10 data loss incident): mid-name mm values are
        # layer heights in non-convention names — never strip them.
        "Voron0.2 - 0.20mm - CF - Functional",
        "Voron0.2 - 0.20mm - Speed",
        "Nozzle 0.2 - 0.10mm - silk",
    ])
    def test_does_not_touch_layer_heights_or_minimal_nozzles(self, input_name):
        assert _normalize_name(input_name) == input_name

    def test_layer_height_padding_and_nozzle_stripping_coexist(self):
        assert (
            _normalize_name("0.2mm - Draft (Doomcube - 0.40mm)")
            == "0.20mm - Draft (Doomcube - 0.4mm)"
        )


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


class TestAppendHardware:
    """Filaments linked to printers but missing the (Extruder - Hotend -
    NozzleSize) parenthetical get it appended from their machine."""

    MACHINE_HW = {
        "Bambu Lab X1 Carbon - Pika - 0.4mm": "Pika - 0.4mm",
        "Doomcube - WWBMG - TeaKettle - 0.4mm": "WWBMG - TeaKettle - 0.4mm",
        "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm": "LGX Lite Pro - TeaKettle - 0.4mm",
    }

    def _filament(self, name, printers):
        from orcaslicer_cleaner.standardizer import _append_hardware
        p = Profile(
            name=name, category=ProfileCategory.FILAMENT, directory=Path("/tmp"),
            settings={"compatible_printers": printers},
        )
        return _append_hardware(name, p, self.MACHINE_HW)

    def test_appends_hardware_from_single_machine(self):
        result = self._filament("PolyTerra PLA - Black", ["Bambu Lab X1 Carbon - Pika - 0.4mm"])
        assert result == "PolyTerra PLA - Black (Pika - 0.4mm)"

    def test_same_hw_across_machines_ok(self):
        # different machines, identical hardware path -> unambiguous
        hw = dict(self.MACHINE_HW)
        hw["Voron - WWBMG - TeaKettle - 0.4mm"] = "WWBMG - TeaKettle - 0.4mm"
        from orcaslicer_cleaner.standardizer import _append_hardware
        p = Profile(
            name="ASA - 3DO", category=ProfileCategory.FILAMENT, directory=Path("/tmp"),
            settings={"compatible_printers": [
                "Doomcube - WWBMG - TeaKettle - 0.4mm", "Voron - WWBMG - TeaKettle - 0.4mm",
            ]},
        )
        assert _append_hardware("ASA - 3DO", p, hw) == "ASA - 3DO (WWBMG - TeaKettle - 0.4mm)"

    def test_ambiguous_hardware_skipped(self):
        result = self._filament("ASA - 3DO", [
            "Doomcube - WWBMG - TeaKettle - 0.4mm",
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
        ])
        assert result is None

    def test_existing_parenthetical_skipped(self):
        assert self._filament(
            "PolyMaker - PolyTerra PLA+ (Satin PLA)", ["Bambu Lab X1 Carbon - Pika - 0.4mm"]
        ) is None

    def test_empty_cp_skipped(self):
        assert self._filament("PolyTerra PLA - Black", []) is None

    def test_machine_without_hw_segment_skipped(self):
        # e.g. "Snapmaker U1 - 0.4mm" resolves to no hardware path
        assert self._filament("HTPLA - Protopasta", ["Snapmaker U1 - 0.4mm"]) is None


class TestCustomBracketRendering:
    """Phase 3: name-building honors the configured hardware bracket/separator.

    The key safety property: a format that uses [square] brackets must NOT make
    the append helper (which looks for a trailing hardware bracket) fail to see
    an existing [bracket] and corrupt the name by appending a second (paren)."""

    MACHINE_HW = {"Bambu Lab X1 Carbon - Pika - 0.4mm": "Pika - 0.4mm"}

    def _spec(self):
        from orcaslicer_cleaner.naming import render_spec
        return render_spec("{material} - {brand} [{hardware}]")

    def _profile(self, name, printers):
        return Profile(
            name=name, category=ProfileCategory.FILAMENT, directory=Path("/tmp"),
            settings={"compatible_printers": printers},
        )

    def test_append_uses_configured_bracket(self):
        from orcaslicer_cleaner.standardizer import _append_hardware
        p = self._profile("PolyTerra PLA - Black", ["Bambu Lab X1 Carbon - Pika - 0.4mm"])
        assert _append_hardware(p.name, p, self.MACHINE_HW, self._spec()) == (
            "PolyTerra PLA - Black [Pika - 0.4mm]"
        )

    def test_existing_square_bracket_not_double_appended(self):
        # Regression: with a [square] format, a name already ending in [hw]
        # must be left alone — not get a (paren) tacked on.
        from orcaslicer_cleaner.standardizer import _append_hardware
        p = self._profile("PLA - X [Pika - 0.4mm]", ["Bambu Lab X1 Carbon - Pika - 0.4mm"])
        assert _append_hardware(p.name, p, self.MACHINE_HW, self._spec()) is None

    def test_inject_uses_configured_bracket(self):
        from orcaslicer_cleaner.standardizer import _inject_hardware
        p = self._profile("PLA - X [0.4mm]", ["Bambu Lab X1 Carbon - Pika - 0.4mm"])
        assert _inject_hardware(p, self.MACHINE_HW, self._spec()) == "PLA - X [Pika - 0.4mm]"

    def test_inject_hardware_path_with_backslash_not_interpreted(self):
        # Defensive: a hardware string with regex-replacement metachars must be
        # inserted literally, not interpreted by re.sub.
        from orcaslicer_cleaner.standardizer import _inject_hardware
        hw = {"M": r"a\1b - 0.4mm"}
        p = self._profile("PLA - X [0.4mm]", ["M"])
        assert _inject_hardware(p, hw, self._spec()) == r"PLA - X [a\1b - 0.4mm]"
