"""Reference (ideal) ranges for common health metrics — DRAFT.

DRAFT — pending clinician sign-off. Adult, general-population typical ranges
used for DECISION SUPPORT ONLY: to tell a reader whether a value they report
sits inside or outside the usual range and to route them to a clinician when it
does not. These are NOT diagnostic thresholds and the code never labels a value
as a disease — a single reading, out of clinical context, cannot diagnose.

Pure/stdlib-only and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RangeSpec:
    unit: str
    low: float | None   # None → no lower bound is flagged
    high: float | None  # None → no upper bound is flagged
    label: str
    note: str = ""      # extra context (e.g. fasting vs post-meal)


# key → RangeSpec (DRAFT — pending clinician sign-off).
RANGES: dict[str, RangeSpec] = {
    # Glucose: the generic "sugar" value is interpreted against the FASTING
    # reference, with a note that the target differs after meals.
    "blood_sugar": RangeSpec(
        "mg/dL", 70, 99, "fasting blood sugar",
        note="Targets differ by timing — a fasting reading is judged against "
             "70–99 mg/dL, while up to ~140 mg/dL can be usual two hours after "
             "a meal. A doctor can tell which applies to your reading.",
    ),
    "fasting_glucose": RangeSpec("mg/dL", 70, 99, "fasting blood sugar"),
    "random_glucose": RangeSpec(
        "mg/dL", 70, 140, "post-meal blood sugar",
        note="This is the usual range up to about two hours after eating.",
    ),
    "hba1c": RangeSpec(
        "%", None, 5.7, "HbA1c",
        note="HbA1c reflects average glucose over about three months.",
    ),
    # Labelled "heart rate", not "resting heart rate". The band IS the resting
    # one, but this label is READER-FACING copy, and calling a figure "a
    # resting heart rate" is the one thing the wearable-grading guard exists to
    # stop -- so the fallback path was emitting that exact sentence, with a
    # threshold attached, whenever the model spelled the metric differently.
    "heart_rate": RangeSpec("bpm", 60, 100, "heart rate"),
    "spo2": RangeSpec("%", 95, None, "oxygen saturation (SpO2)"),
    "total_cholesterol": RangeSpec("mg/dL", None, 200, "total cholesterol"),
    "ldl": RangeSpec("mg/dL", None, 100, "LDL cholesterol"),
    "hdl": RangeSpec(
        "mg/dL", 40, None, "HDL cholesterol",
        note="Higher HDL is generally better; the usual floor is ~40 mg/dL "
             "(a little higher for women).",
    ),
    "hemoglobin": RangeSpec(
        "g/dL", 12, 17, "hemoglobin",
        note="The usual range differs by sex (about 12–15 for women, 13–17 "
             "for men).",
    ),
    "bmi": RangeSpec(
        "kg/m²", 18.5, 25, "BMI",
        note="For people of South Asian descent, some guidelines use a lower "
             "overweight cut-off of about 23.",
    ),
}

# Blood pressure is two numbers; handled specially.
BP_SYSTOLIC = RangeSpec("mmHg", 90, 120, "systolic blood pressure")
BP_DIASTOLIC = RangeSpec("mmHg", 60, 80, "diastolic blood pressure")
_BP_RANGE_TEXT = "around 90–120/60–80 mmHg"


@dataclass(frozen=True)
class RangeVerdict:
    status: str          # "in_range" | "above" | "below"
    label: str
    range_text: str      # human-readable typical range, e.g. "70–99 mg/dL"
    note: str = ""


def _range_text(spec: RangeSpec) -> str:
    if spec.low is not None and spec.high is not None:
        return f"{_n(spec.low)}–{_n(spec.high)} {spec.unit}"
    if spec.high is not None:
        return f"below {_n(spec.high)} {spec.unit}"
    if spec.low is not None:
        return f"at or above {_n(spec.low)} {spec.unit}"
    return ""


def _n(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def classify(metric_key: str, value: float) -> RangeVerdict | None:
    """Classify a single-value metric against its reference range."""
    spec = RANGES.get(metric_key)
    if spec is None:
        return None
    if spec.high is not None and value > spec.high:
        status = "above"
    elif spec.low is not None and value < spec.low:
        status = "below"
    else:
        status = "in_range"
    return RangeVerdict(status, spec.label, _range_text(spec), spec.note)


def classify_bp(systolic: float, diastolic: float | None) -> RangeVerdict:
    """Classify a blood-pressure reading (systolic, optional diastolic)."""
    sys_high = BP_SYSTOLIC.high is not None and systolic > BP_SYSTOLIC.high
    sys_low = BP_SYSTOLIC.low is not None and systolic < BP_SYSTOLIC.low
    dia_high = (
        diastolic is not None and BP_DIASTOLIC.high is not None
        and diastolic > BP_DIASTOLIC.high
    )
    dia_low = (
        diastolic is not None and BP_DIASTOLIC.low is not None
        and diastolic < BP_DIASTOLIC.low
    )
    if sys_high or dia_high:
        status = "above"
    elif sys_low or dia_low:
        status = "below"
    else:
        status = "in_range"
    return RangeVerdict(status, "blood pressure", _BP_RANGE_TEXT)
