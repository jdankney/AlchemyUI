"""The admin page's GPU reading, including the case that cost a live run a
10x slowdown: a laptop on battery, its GPU capped at 10 of 80 watts."""

from __future__ import annotations

from alchemyui.power import parse_stats


def test_a_normal_reading():
    s = parse_stats(["1254", "6144", "57", "71.3", "100", "80.00", "80.00"])
    assert s["available"] and s["power_w"] == 71.3 and s["vram_total_mib"] == 6144
    assert s["power_limit_w"] == 80.0 and s["capped"] is False


def test_a_power_capped_gpu_says_so():
    # measured on this laptop unplugged: P8, 300 MHz, "SW Power Cap: Active"
    s = parse_stats(["5550", "6144", "57", "12.77", "100", "10.00", "80.00"])
    assert s["capped"] is True and s["power_limit_w"] == 10.0 and s["default_limit_w"] == 80.0


def test_a_gpu_that_cannot_report_its_limit_is_not_called_capped():
    s = parse_stats(["100", "6144", "40", "5.0", "0", "[N/A]", "[N/A]"])
    assert s["available"] and s["power_limit_w"] is None and s["capped"] is False


def test_no_reading_at_all():
    assert parse_stats(None) == {"available": False}
