"""Recovered anti-evil arithmetic, separate from player source completeness.

01AE7C80 adds template + max(upgrade, forging); 01F49D70's stat49 branch
adds a truncated flat value or a float32 percentage delta. 015BB170 then
multiplies the integer base by the stored float32 rate and truncates via
031C5390. Callers must provide the actual ordered, active contributions.
This module neither enumerates those sources nor produces a JoinProfile.
"""
from dataclasses import dataclass
from math import isfinite
from struct import pack, unpack


def _int32(value):
    if type(value) is not int or not -0x80000000 <= value <= 0x7fffffff:
        raise ValueError('authoritative anti-evil signed integer unavailable')
    return value


def _float32(value):
    if type(value) not in (int, float):
        raise ValueError('authoritative anti-evil finite float unavailable')
    try:
        result = unpack('<f', pack('<f', value))[0]
    except (OverflowError, ValueError):
        raise ValueError('anti-evil float32 overflow') from None
    if not isfinite(result):
        raise ValueError('anti-evil float32 must be finite')
    return result


def equipment_anti_evil(*, template, upgrade, forging):
    """Use resolved contributions; an absent enhancement is not guessed as zero."""
    for value in (template, upgrade, forging):
        if _int32(value) < 0:
            raise ValueError('equipment anti-evil contribution cannot be negative')
    return _int32(template + max(upgrade, forging))


@dataclass(frozen=True)
class AntiEvilStats:
    base: int
    multiplier: float

    def __post_init__(self):
        _int32(self.base)
        object.__setattr__(self, 'multiplier', _float32(self.multiplier))

    @classmethod
    def reset(cls):
        """02E9B640 resets this computation object to base0/rate1."""
        return cls(base=0, multiplier=1.0)

    def add_equipment(self, contribution):
        if _int32(contribution) < 0:
            raise ValueError('equipment anti-evil contribution cannot be negative')
        return AntiEvilStats(_int32(self.base + contribution), self.multiplier)

    def apply_effect(self, *, percentage, amount):
        if type(percentage) is not bool:
            raise ValueError('resolved anti-evil effect mode unavailable')
        amount = _float32(amount)
        if percentage:
            delta = _float32(amount * _float32(0.01))
            return AntiEvilStats(self.base, _float32(self.multiplier + delta))
        # CVTTSS2SI truncates each flat source before the signed integer ADD.
        # Overflow is unsupported input, never a fabricated valid profile.
        return AntiEvilStats(_int32(self.base + _int32(int(amount))), self.multiplier)

    def value(self):
        # Keep the product in binary64 as at 031C5390's FSTP/CVTTSD2SI.
        # An extra float32 round here changes legitimate boundary values.
        return _int32(int(self.base * self.multiplier))
