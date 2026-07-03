import re


def parse_spec_capacity(spec: str | None) -> float | None:
    """Parse a BMS spec string (e.g. '48V/100AH') and return the kWh capacity.

    Handles mixed case and optional whitespace around the separator.
    Returns None when the string is absent or empty.
    Raises ValueError when the string is present but cannot be parsed.

    Examples::

        '48V/100AH' -> 4.8
        '48V/74AH'  -> 3.55
        '48V/50AH'  -> 2.4
    """
    if not spec:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Vv]\s*/\s*(\d+(?:\.\d+)?)\s*[Aa][Hh]", spec)
    if not m:
        raise ValueError(f"Cannot parse battery spec: {spec!r}")
    return round(float(m.group(1)) * float(m.group(2)) / 1000.0, 2)
