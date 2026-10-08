"""A4 VIN validation, A5 OBD-II DTC parsing.

Both run on the ingest hot path, once per event at 100k events/sec, so both are O(1) with no
allocation in the common case. Pre-compiled patterns are module-level on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# ISO 3779: 17 characters, and I, O and Q are excluded because they are visually ambiguous
# with 1 and 0. A regex that merely checks [A-Z0-9]{17} accepts VINs no manufacturer issues.
VIN_RE: Final = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# Transliteration table for the ISO 3779 check digit. Note I, O, Q are absent by construction.
_TRANSLITERATION: Final[dict[str, int]] = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}

# Positional weights; position 9 (index 8) is the check digit itself and carries weight 0.
_WEIGHTS: Final[tuple[int, ...]] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit(vin: str) -> str:
    """Return the ISO 3779 check digit for `vin`. 'X' represents a remainder of 10.

    Time O(17) = O(1). Space O(1).
    """
    total = sum(_TRANSLITERATION[ch] * w for ch, w in zip(vin, _WEIGHTS))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_valid_vin(vin: str, *, require_check_digit: bool = True) -> bool:
    """Validate shape and, by default, the check digit.

    `require_check_digit` exists because the check digit is mandatory for North American VINs but
    is only advisory in some other markets, and a few real OEM feeds carry VINs that fail it. We
    default to strict and let the ingest adapter relax it per OEM dialect rather than silently
    accepting everything.

    Time O(1). Space O(1).
    """
    if not VIN_RE.match(vin):
        return False
    if not require_check_digit:
        return True
    return vin[8] == vin_check_digit(vin)


# OBD-II / SAE J2012 DTC: one system letter, then 4 hex-ish digits.
#   P powertrain, B body, C chassis, U network
#   second char 0-3 encodes generic (0,2) vs manufacturer-specific (1,3)
DTC_RE: Final = re.compile(r"^(?P<system>[PBCU])(?P<kind>[0-3])(?P<subsystem>[0-9A-F])(?P<fault>[0-9A-F]{2})$")

_SYSTEM_NAME: Final[dict[str, str]] = {
    "P": "powertrain", "B": "body", "C": "chassis", "U": "network",
}

# Severity drives which lane the event takes (CRITICAL goes to the priority topic), so this map
# is a product decision, not a lookup detail. Anything unmapped defaults to MEDIUM, never LOW,
# so that a code we have not classified cannot silently vanish from the board.
_SEVERITY: Final[dict[str, str]] = {
    "P0217": "CRITICAL",  # engine overtemperature
    "P0300": "HIGH",      # random/multiple cylinder misfire
    "P0301": "HIGH",      # cylinder 1 misfire
    "P0128": "MEDIUM",    # coolant thermostat below regulating temperature
    "P0420": "MEDIUM",    # catalyst efficiency below threshold
    "B0001": "CRITICAL",  # driver airbag circuit
    "U0100": "CRITICAL",  # lost communication with ECM/PCM
    "P0A80": "HIGH",      # replace hybrid battery pack
}


@dataclass(frozen=True, slots=True)
class Dtc:
    code: str
    system: str
    manufacturer_specific: bool
    severity: str


def parse_dtc(raw: str) -> Dtc | None:
    """Parse one DTC. Returns None for anything that is not a well-formed code.

    Returning None rather than raising is deliberate: at 100k events/sec a malformed code from a
    new OEM dialect must not raise into the hot path. The caller counts rejects and routes the
    envelope to the DLQ for replay once the mapping is fixed.

    Time O(1). Space O(1).
    """
    m = DTC_RE.match(raw.strip().upper())
    if m is None:
        return None
    code = m.group(0)
    return Dtc(
        code=code,
        system=_SYSTEM_NAME[m.group("system")],
        manufacturer_specific=m.group("kind") in ("1", "3"),
        severity=_SEVERITY.get(code, "MEDIUM"),
    )


def parse_dtc_list(raw_codes: list[str]) -> tuple[list[Dtc], list[str]]:
    """Parse many codes, returning (parsed, rejected). Time O(n). Space O(n)."""
    parsed: list[Dtc] = []
    rejected: list[str] = []
    for raw in raw_codes:
        dtc = parse_dtc(raw)
        if dtc is None:
            rejected.append(raw)
        else:
            parsed.append(dtc)
    return parsed, rejected
