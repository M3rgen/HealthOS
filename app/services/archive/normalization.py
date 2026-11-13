"""Map pure parsed records to SQL contracts inside the caller's transaction.

The parser owns source field precedence and units. This adapter owns ORM selection,
SQL numeric bounds and optional column limits; it never commits or catches DB errors.
"""

from dataclasses import dataclass
from decimal import Context, DecimalException, localcontext
from typing import Any

from sqlalchemy.orm import DeclarativeBase

from app.core.fit import FIT_NORMALIZED, FIT_RECORD_PREFIX
from app.core.normalization import prepare_numeric
from app.core.providers import FIT, GARMIN
from app.core.records import RecordType
from app.models.health import (
    ActiveZoneMinute,
    Distance,
    EnergyExpenditure,
    ExerciseSession,
    HeartRate,
    HeartRateVariability,
    OxygenSaturation,
    RespiratoryRate,
    RestingHeartRate,
    ScalarMetric,
    SleepSession,
    Steps,
    Temperature,
    Vo2Max,
    Weight,
)
from app.parsers.common import payload_for, source_id
from app.parsers.normalize import normalize as parse
from app.parsers.text import prepare_optional_text

_MODELS: dict[str, type[DeclarativeBase]] = {
    model.__name__: model
    for model in (
        ActiveZoneMinute,
        Distance,
        EnergyExpenditure,
        ExerciseSession,
        HeartRate,
        HeartRateVariability,
        OxygenSaturation,
        RespiratoryRate,
        RestingHeartRate,
        ScalarMetric,
        SleepSession,
        Steps,
        Temperature,
        Vo2Max,
        Weight,
    )
}


@dataclass(frozen=True)
class NormalizedRecord:
    model: type[DeclarativeBase]
    values: dict[str, Any]


def normalize(spec: RecordType, record: object) -> NormalizedRecord | None:
    try:
        with localcontext(Context()):
            fit_kind = FIT_NORMALIZED.get(spec.name)
            if fit_kind is not None and isinstance(record, dict):
                target = RecordType(fit_kind)
                parsed = parse(target, {**record, target.json_key: payload_for(spec, record)})
                if parsed is not None:
                    source = record.get("dataSource")
                    parsed.values.update(
                        provider=FIT
                        if isinstance(source, dict) and source.get("platform") == FIT
                        else GARMIN,
                        source_record_id=FIT_RECORD_PREFIX + source_id(spec, record),
                    )
            else:
                parsed = parse(spec, record)
            if parsed is None:
                return None
            model = _MODELS[parsed.kind]
            prepare_numeric(model, parsed.values)
            prepare_optional_text(model, parsed.values)
            return NormalizedRecord(model, parsed.values)
    except DecimalException:
        return None
