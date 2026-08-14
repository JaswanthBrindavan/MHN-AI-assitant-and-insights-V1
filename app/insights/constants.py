"""Clinical constants for the insights engine.

DRAFT — pending clinician sign-off.

Every value here is a clinical modelling choice (onset midpoints, certainty
weights, standard safety copy) and must be reviewed and signed off by a
qualified clinician before any non-synthetic use. Nothing here is diagnosis.
"""

from __future__ import annotations

# --- Onset bands → representative midpoint age (years) -----------------------
# Midpoints are chosen to sit inside each band so threshold comparisons never
# land on a boundary. "unknown" has no usable age → None.
ONSET_MIDPOINTS: dict[str, int | None] = {
    "under_30": 25,
    "30_34": 32,
    "35_39": 37,
    "40_44": 42,
    "45_49": 47,
    "50_54": 52,
    "55_59": 57,
    "60_64": 62,
    "65_69": 67,
    "70_plus": 75,
    "unknown": None,
}

# --- Certainty weighting -----------------------------------------------------
CERTAINTY_WEIGHTS: dict[str, float] = {
    "verified": 1.0,
    "confirmed": 1.0,
    "as_far_as_i_know": 0.6,
}

# --- Pedigree slot taxonomy --------------------------------------------------
PARENT_SLOTS: tuple[str, ...] = ("mother", "father")
GRANDPARENT_SLOTS: tuple[str, ...] = (
    "grandmother_maternal",
    "grandfather_maternal",
    "grandmother_paternal",
    "grandfather_paternal",
)

# Each grandparent's own child (the "linking parent") — used for vertical-chain
# detection (grandparent AND that grandparent's child both affected).
GRANDPARENT_TO_PARENT: dict[str, str] = {
    "grandmother_maternal": "mother",
    "grandfather_maternal": "mother",
    "grandmother_paternal": "father",
    "grandfather_paternal": "father",
}

# Deterministic ordering + human labels for the evidence line.
EVIDENCE_SLOT_ORDER: tuple[str, ...] = (
    "father",
    "mother",
    "grandfather_paternal",
    "grandmother_paternal",
    "grandfather_maternal",
    "grandmother_maternal",
)
SLOT_DISPLAY: dict[str, str] = {
    "mother": "mother",
    "father": "father",
    "grandmother_maternal": "maternal grandmother",
    "grandfather_maternal": "maternal grandfather",
    "grandmother_paternal": "paternal grandmother",
    "grandfather_paternal": "paternal grandfather",
}

# --- Standard safety copy (filled into every rendered insight) ---------------
# DRAFT copy. These are the {not_a_diagnosis} and {next_step} fills; the
# renderer refuses any template missing either placeholder.
NOT_A_DIAGNOSIS_TEXT: str = (
    "This is not a diagnosis. It is a decision-support note based only on the "
    "family history you shared, meant to help you and a clinician decide what to "
    "look at together."
)
NEXT_STEP_TEXT: str = (
    "A good next step is to mention this family history to your doctor at your "
    "next visit, so they can advise what (if any) checks make sense for you."
)
# Included in any insight or reply that touches medication.
MEDICATION_NOTE: str = (
    "Please do not stop or change any medication or dose on your own — discuss "
    "it with the prescriber first."
)
