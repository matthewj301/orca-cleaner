"""Tests for _machine_matches_hardware in cleaner.py.

Regression tests for the comma-separated hardware hint tokenization bug
where 'LGX Lite Pro, TeaKettle 0.40mm' was treated as a single token,
causing false positives via bidirectional substring matching.
"""

import pytest

from orcaslicer_cleaner.cleaner import _machine_matches_hardware


MACHINES = [
    "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
    "Doomcube - WWBMG - TeaKettle - 0.5mm",
    "Voron 0.1rc2 - Sherpa Mini 10t - TeaKettle - 0.4mm",
    "Snapmaker U1 - 0.40mm",
    "Bambu Lab X1 Carbon - 0.4mm",
    "Positron - Sherpa Micro - 0.4mm",
    "RatRig V-Core 3.1 - LGX Pro Metal - Chube Conduction - 0.5mm",
    "Annex K3 - Sherpa Mini - Chube Air - 0.5mm",
]


class TestCommaDelimitedHints:
    """Hardware hints using commas instead of hyphens (e.g. PETG profiles)."""

    def test_lgx_lite_pro_teakettle_matches_correct_machine(self):
        assert _machine_matches_hardware(
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
            "LGX Lite Pro, TeaKettle 0.40mm",
        )

    def test_lgx_lite_pro_teakettle_rejects_wwbmg(self):
        assert not _machine_matches_hardware(
            "Doomcube - WWBMG - TeaKettle - 0.5mm",
            "LGX Lite Pro, TeaKettle 0.40mm",
        )

    def test_lgx_lite_pro_teakettle_rejects_voron(self):
        assert not _machine_matches_hardware(
            "Voron 0.1rc2 - Sherpa Mini 10t - TeaKettle - 0.4mm",
            "LGX Lite Pro, TeaKettle 0.40mm",
        )

    def test_lgx_lite_pro_teakettle_rejects_snapmaker(self):
        assert not _machine_matches_hardware(
            "Snapmaker U1 - 0.40mm",
            "LGX Lite Pro, TeaKettle 0.40mm",
        )

    def test_lgx_lite_pro_teakettle_rejects_bambu(self):
        assert not _machine_matches_hardware(
            "Bambu Lab X1 Carbon - 0.4mm",
            "LGX Lite Pro, TeaKettle 0.40mm",
        )


class TestHyphenDelimitedHints:
    """Hardware hints using hyphens (e.g. ASA profiles)."""

    def test_lgx_lite_pro_teakettle_hyphen_matches_correct(self):
        assert _machine_matches_hardware(
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
            "LGX Lite Pro - TeaKettle - 0.40mm",
        )

    def test_lgx_lite_pro_teakettle_hyphen_rejects_wwbmg(self):
        assert not _machine_matches_hardware(
            "Doomcube - WWBMG - TeaKettle - 0.5mm",
            "LGX Lite Pro - TeaKettle - 0.40mm",
        )

    def test_sherpa_mini_chube_air_matches_annex(self):
        assert _machine_matches_hardware(
            "Annex K3 - Sherpa Mini - Chube Air - 0.5mm",
            "Sherpa Mini 8t - Chube Air - 0.50mm",
        )

    def test_sherpa_mini_chube_air_rejects_ratrig(self):
        assert not _machine_matches_hardware(
            "RatRig V-Core 3.1 - LGX Pro Metal - Chube Conduction - 0.5mm",
            "Sherpa Mini 8t - Chube Air - 0.50mm",
        )


class TestAliases:
    """Hardware aliases like 'mako' -> 'bambu'."""

    def test_mako_matches_bambu(self):
        assert _machine_matches_hardware(
            "Bambu Lab X1 Carbon - 0.4mm",
            "Mako",
        )

    def test_mako_rejects_non_bambu(self):
        assert not _machine_matches_hardware(
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm",
            "Mako",
        )


class TestDirectSubstring:
    """Direct substring match (simplest case)."""

    def test_positron_matches_positron_machine(self):
        assert _machine_matches_hardware(
            "Positron - Sherpa Micro - 0.4mm",
            "Positron",
        )

    def test_positron_rejects_other_machines(self):
        for machine in MACHINES:
            if "Positron" not in machine:
                assert not _machine_matches_hardware(machine, "Positron")


class TestNozzleOnlyHints:
    """Nozzle-only hints hit the direct substring path. In practice,
    _extract_hardware_hint filters these out before calling _machine_matches_hardware,
    but verify the token path rejects them when the substring doesn't match."""

    def test_nozzle_only_rejects_when_no_substring_match(self):
        assert not _machine_matches_hardware(
            "Doomcube - LGX Lite Pro - TeaKettle - 0.4mm", "0.50mm"
        )
        assert not _machine_matches_hardware(
            "Snapmaker U1 - 0.40mm", "0.5mm"
        )
