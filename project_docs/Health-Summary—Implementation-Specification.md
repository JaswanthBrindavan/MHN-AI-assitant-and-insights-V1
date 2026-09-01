# Health Summary — Implementation Specification

## 1. Objective

Build a **Health Summary** capability for the health assistant.

The Health Summary represents the user's **overall/current health picture**, rather than a summary of a single week.

It should answer questions such as:

- "What's my health summary?"
- "Give me my health summary."
- "What do you know about my health?"
- "Summarize my health."
- "What are my current health conditions?"
- "What medications am I taking?"

The Health Summary must provide a concise, clinically useful snapshot of the user's health context.

It must NOT be a raw dump of every medical record.

---

# 2. Health Summary vs Weekly Health Review

These are separate concepts.

## Health Summary

Focuses on the user's broader and relatively persistent health context:

- Demographics
- Active conditions
- Medical history
- Medications
- Allergies
- Important procedures/surgeries
- Relevant family history
- Important clinical measurements
- Important recent labs
- Relevant preventive care
- Important ongoing symptoms
- Relevant health considerations

## Weekly Health Review

Focuses specifically on what happened during a particular week:

- Symptoms reported during the week
- Wearable metrics
- Sleep
- Activity
- Vitals
- Hydration
- Caffeine
- Lifestyle
- Trends
- Cross-metric correlations
- Weekly graphs

Do not combine these into one feature.

---

# 3. Inspect Existing Code First

Before implementing anything:

1. Inspect the existing project structure.
2. Identify existing health-summary functionality.
3. Identify existing patient/user models.
4. Identify condition/diagnosis models.
5. Identify medication models.
6. Identify allergy models.
7. Identify symptom models.
8. Identify procedure/surgery models.
9. Identify family-history models.
10. Identify vitals/lab models.
11. Identify wearable integrations.
12. Identify existing AI provider abstraction.
13. Identify existing health assistant orchestration.
14. Identify existing DTO/API conventions.
15. Identify existing persistence/caching patterns.
16. Identify existing tests.

Reuse existing infrastructure wherever possible.

Do not create duplicate models or services.

Do not rewrite unrelated functionality.

---

# 4. Health Summary Categories

The summary should support the following categories.

## A. Patient Basics

Where available:

- Age
- Sex
- Height
- Weight
- BMI
- Relevant lifestyle information

Do not expose unnecessary personal information.

Only include fields that are useful for health context.

---

# 5. Active Health Conditions

Include documented health conditions.

For each condition, include where available:

- Condition name
- Status
- Diagnosis date
- Severity/stage
- Current management
- Relevant recent measurements
- Relevant notes

Possible statuses:

- Active
- Controlled
- Improving
- Stable
- Resolved
- Historical

Do not infer diagnoses from symptoms or wearable measurements.

Only documented diagnoses should appear as diagnoses.

Example:

> Hypertension — active, currently treated.

Do NOT generate:

> Possible hypertension.

from a blood-pressure reading alone.

---

# 6. Medications

Include current medications first.

For each medication, include where available:

- Medication name
- Dose
- Frequency
- Route
- Indication
- Start date
- End date
- Current status

Separate:

- Current medications
- Recently stopped medications
- Historical medications

Do not infer that a medication is current simply because it exists in historical records.

---

# 7. Allergies

Allergies should have high visibility.

Include:

- Allergen
- Reaction
- Severity
- Status

Example:

> Penicillin — rash.

Do not confuse medication side effects with allergies unless the underlying data explicitly classifies them as allergies.

---

# 8. Symptoms

Include ongoing or clinically relevant symptoms.

Important distinction:

**A symptom is not a diagnosis.**

For example:

```text
Symptom:
Headache

Diagnosis:
Migraine
```

These must remain separate.

Include:

- Symptom
- Duration
- Frequency
- Severity
- Current status
- Relevant notes

Prioritize ongoing/recent significant symptoms.

Do not include every historical symptom indefinitely.

---

# 9. Medical History

Include clinically relevant historical information such as:

- Previous diagnoses
- Significant illnesses
- Hospitalizations
- Procedures
- Surgeries
- Important medical events

Prioritize information that could affect current care.

Do not overload the summary with irrelevant historical records.

---

# 10. Procedures & Surgeries

Include important procedures/surgeries:

- Procedure
- Date
- Relevant outcome/notes where available

Example:

> Appendectomy — 2019.

---

# 11. Family History

Include relevant documented family history.

Examples:

- Diabetes
- Hypertension
- Cardiovascular disease
- Stroke
- Cancer
- Genetic conditions

Include relationship where available.

Do not invent family history.

---

# 12. Clinical Measurements

Include clinically relevant measurements where available.

Potential data:

- Blood pressure
- Heart rate
- Weight
- SpO2
- Temperature
- Respiratory rate
- Relevant lab values
- Other clinically important measurements

Prioritize:

1. Recent values
2. Meaningful trends
3. Measurements related to active conditions

Do not dump every historical measurement.

---

# 13. Laboratory Results

Include relevant recent laboratory results.

Potential examples:

- HbA1c
- Glucose
- Lipids
- CBC
- Creatinine
- eGFR
- Liver function
- Thyroid function
- Other condition-specific tests

Where historical values exist, show meaningful trends.

Example:

> HbA1c: 7.2%, down from 7.8% six months earlier.

Do not interpret lab results as diagnoses unless that diagnosis is already documented or appropriate clinical logic explicitly exists.

---

# 14. Preventive Care

If supported by the application, include:

- Vaccinations
- Screening tests
- Preventive examinations
- Relevant overdue preventive care

Only include this when the application has reliable data.

---

# 15. Wearable Data

Wearable data generally belongs in the **Weekly Health Review**, not the persistent Health Summary.

However, important longer-term trends may be included when clinically/relevantly useful.

For example:

> Resting heart rate has remained stable over the past 4 weeks.

Do not fill the Health Summary with daily wearable data.

---

# 16. Generated Summary

The generated Health Summary should have a concise narrative.

Example:

> You have two currently documented chronic conditions: hypertension and type 2 diabetes. Your current medications include amlodipine and metformin. Your recent HbA1c has improved compared with the previous measurement, while your blood pressure has remained relatively stable. You have a documented penicillin allergy and no other significant recent health events recorded.

The exact text must come from real data.

Do not hardcode medical facts.

---

# 17. Structured Response

Prefer a structured response rather than one large text string.

Example:

```json
{
  "patient": {},
  "activeConditions": [],
  "medications": [],
  "allergies": [],
  "symptoms": [],
  "medicalHistory": [],
  "procedures": [],
  "familyHistory": [],
  "recentVitals": [],
  "recentLabs": [],
  "preventiveCare": [],
  "summary": "...",
  "keyConsiderations": []
}
```

Adapt this to the application's existing DTO conventions.

Do not create duplicate DTOs if an appropriate structure already exists.

---

# 18. Fact vs Interpretation

The Health Summary must distinguish between:

### Documented facts

Information directly recorded in the user's data.

Example:

> Hypertension — diagnosed 2021.

### Computed information

Information calculated from existing records.

Example:

> Weight increased by 3 kg over six months.

### AI observations

Interpretations generated from the available information.

Example:

> Recent weight gain may be worth discussing during your next routine health review.

Do not represent AI observations as documented clinical facts.

---

# 19. Safety Requirements

The Health Summary is a health-related feature.

The system must:

- Never fabricate medical information.
- Never invent diagnoses.
- Never invent medications.
- Never invent allergies.
- Never invent lab results.
- Never turn symptoms into diagnoses.
- Never claim a wearable measurement establishes a diagnosis.
- Clearly distinguish patient-reported information from documented clinical information.
- Avoid unsupported medical conclusions.
- Avoid unnecessary alarm.
- Surface genuinely important documented health information appropriately.

---

# 20. Missing Data

Missing data must not be treated as negative information.

For example:

Incorrect:

> No allergies.

when allergy information simply does not exist.

Correct:

> No allergy information recorded.

Likewise:

Incorrect:

> No medications.

when medication data is unavailable.

Correct:

> Medication information is not available.

Distinguish:

- No known/documented data
- Explicitly negative information
- Missing information

---

# 21. Date and Time

Use the application's established timezone/date utilities.

Do not mix UTC and local dates incorrectly.

All historical information should preserve its actual date where relevant.

---

# 22. AI Generation

Use the application's existing AI provider abstraction.

Do not directly instantiate an AI provider inside the controller.

Prefer:

```text
Controller
    ↓
HealthSummaryService
    ↓
Data aggregation
    ↓
HealthSummaryAnalyzer
    ↓
AI generation service
    ↓
Structured HealthSummary
```

Follow the existing architecture instead of blindly creating these exact classes.

---

# 23. Caching / Regeneration

If the application already supports generated-summary persistence/caching, reuse it.

Regenerate when relevant underlying data changes, including:

- New diagnosis
- Medication change
- New allergy
- New significant symptom
- New lab result
- New procedure
- Other important clinical updates

Do not introduce a new caching system if the existing system already handles this.

---

# 24. Testing

Add tests covering:

### Conditions

- Active conditions are included.
- Historical conditions are appropriately separated.
- Symptoms are not converted into diagnoses.

### Medications

- Current medications are correctly identified.
- Historical medications are not incorrectly shown as current.

### Allergies

- Allergies are included correctly.
- Missing allergy data is distinguished from no known allergies.

### Labs/vitals

- Recent values are selected correctly.
- Meaningful trends are calculated correctly.

### Missing data

- Missing fields do not become fabricated values.
- Empty sections are handled correctly.

### AI

- AI receives only real data.
- Generated output is validated.
- Invalid AI output is handled safely.

### Regression

Run relevant existing tests and ensure existing health-summary/assistant functionality continues working.

---

# 25. Definition of Done

The Health Summary is complete when:

- It provides a concise overview of the user's broader health context.
- Active conditions are included.
- Current medications are included.
- Allergies are prominently represented.
- Relevant symptoms are included without becoming diagnoses.
- Important medical history is included.
- Procedures/surgeries are included where relevant.
- Family history is included where available.
- Relevant recent vitals/labs are included.
- Meaningful trends are surfaced.
- Wearable data is not unnecessarily dumped into the long-term summary.
- Missing information is handled correctly.
- AI-generated interpretation is clearly distinguishable from source facts.
- No medical information is fabricated.
- Existing architecture and services are reused.
- Tests cover the major paths.

## Core principle

The Health Summary should answer:

> **“What should my health assistant know about my overall health right now?”**

It should be concise, accurate, clinically useful, and grounded entirely in the user's recorded information.