"""Finite read-model arithmetic; retry overflowing intermediates without rounding bounds."""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from math import isfinite
from sys import float_info

from app.parsers.common import number


def finite_number(value: object) -> float | None:
    parsed = number(value)
    if parsed is None or not parsed.is_finite():
        return None
    result = float(parsed)
    return result if isfinite(result) and (result != 0 or parsed == 0) else None


def finite_calculation(value: float, exact: Callable[[], Fraction]) -> float | None:
    """Keep ordinary float results; only retry nonfinite or possibly underflowed results.

    Fractions represent the finite input floats exactly. The retry covers the whole
    expression, so intermediate overflow does not hide a representable final result.
    Only the final conversion can raise OverflowError; no reader exception is swallowed.
    """
    if isfinite(value) and value != 0:
        return value
    precise = exact()
    try:
        result = float(precise)
    except OverflowError:
        return None
    return result if isfinite(result) and (result != 0 or precise == 0) else None


def finite_scale(value: float | Decimal, multiplier: float, divisor: float = 1) -> float | None:
    """Keep source decimals until the exact retry, including the unit conversion.

    A source value may exceed float's range while its scaled result still fits.
    Ordinary finite float arithmetic keeps the existing rounding behavior.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == 0 or multiplier == 0:
            return 0.0 / divisor
        # A finite float shifts the decimal exponent by less than this range.
        # Beyond three ranges, even two scaling operands cannot rescue the result.
        # Reject it before Fraction materializes an enormous power of ten.
        if abs(value.adjusted()) > 3 * (float_info.max_10_exp - float_info.min_10_exp):
            return None
    return finite_calculation(
        float(value) * multiplier / divisor,
        lambda: Fraction(value) * Fraction(multiplier) / Fraction(divisor),
    )


def finite_difference(left: float, right: float) -> float | None:
    return finite_calculation(left - right, lambda: Fraction(left) - Fraction(right))


def finite_sum(values: list[float]) -> float | None:
    return finite_calculation(sum(values), lambda: sum(map(Fraction, values), Fraction()))


@dataclass(slots=True)
class FiniteScaledSum:
    """Stream proportions without retaining operands or rereading their source.

    The compensated float path matches Python 3.12's sum, including the original
    multiply-then-divide rounding. Only a zero/nonfinite result uses the exact sum;
    even an unrepresentable share must participate in cancellation and underflow
    recovery. Keep one Fraction, not a list of contributions, per accumulator.

    Each add performs float operations and rational arithmetic. Rational storage
    and arithmetic cost depend on numerator/denominator bit lengths: distinct
    divisors can grow their common denominator, and totals can grow with count.
    This bounds the number of retained objects, not their byte size or CPU cost.
    """

    _total: float = 0.0
    _correction: float = 0.0
    _exact: Fraction = Fraction()

    def add(self, value: float, multiplier: float, divisor: float) -> None:
        contribution = value * multiplier / divisor
        combined = self._total + contribution
        # Neumaier compensation, as used by sum on the supported Python runtime.
        if abs(self._total) >= abs(contribution):
            self._correction += (self._total - combined) + contribution
        else:
            self._correction += (contribution - combined) + self._total
        self._total = combined
        self._exact += Fraction(value) * Fraction(multiplier) / Fraction(divisor)

    def result(self) -> float | None:
        value = self._total
        # A nonfinite correction must not turn sum's overflowing result into NaN.
        if self._correction and isfinite(self._correction):
            value += self._correction
        return finite_calculation(value, lambda: self._exact)


def finite_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return finite_calculation(
        sum(values) / len(values),
        lambda: sum(map(Fraction, values), Fraction()) / len(values),
    )
