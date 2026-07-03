"""Tests for parse_spec_capacity."""

import pytest

from custom_components.pylontech_mqtt.capacity import parse_spec_capacity


class TestParseSpecCapacity:
    def test_standard_100ah(self):
        assert parse_spec_capacity("48V/100AH") == pytest.approx(4.8)

    def test_standard_50ah(self):
        assert parse_spec_capacity("48V/50AH") == pytest.approx(2.4)

    def test_standard_74ah(self):
        assert parse_spec_capacity("48V/74AH") == pytest.approx(3.55, rel=1e-2)

    def test_lowercase(self):
        assert parse_spec_capacity("48v/100ah") == pytest.approx(4.8)

    def test_mixed_case(self):
        assert parse_spec_capacity("48V/100Ah") == pytest.approx(4.8)

    def test_with_spaces_around_separator(self):
        assert parse_spec_capacity("48V / 100AH") == pytest.approx(4.8)

    def test_decimal_voltage(self):
        assert parse_spec_capacity("51.2V/100AH") == pytest.approx(5.12)

    def test_empty_string_returns_none(self):
        assert parse_spec_capacity("") is None

    def test_none_returns_none(self):
        assert parse_spec_capacity(None) is None

    def test_invalid_format_raises_value_error(self):
        with pytest.raises(ValueError, match="Cannot parse battery spec"):
            parse_spec_capacity("CUSTOM")

    def test_partial_voltage_only_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_spec_capacity("48V only")

    def test_missing_unit_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_spec_capacity("48/100")
