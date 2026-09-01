# Health Correlations & Pattern Analysis — Implementation Specification

## 1. Objective

Build a **Health Correlation & Pattern Analysis** capability for the health assistant.

The system must identify meaningful relationships between:

- Symptoms
- Sleep
- Caffeine
- Hydration
- Activity
- Exercise
- Heart rate
- Resting heart rate
- HRV
- SpO2
- Respiratory rate
- Stress
- Mood
- Alcohol
- Nutrition
- Recovery
- Other supported wearable/lifestyle metrics
- Medication changes, where appropriate

The goal is to identify patterns such as:

> Higher caffeine intake coincided with poorer sleep.

> Headaches occurred more often on days with lower-than-usual hydration.

> Fatigue was reported after nights with shorter sleep.

> Higher stress days also had higher resting heart rate.

The system must **not automatically interpret correlation as causation**.

---

# 2. Important Architecture Principle

Do NOT rely on the LLM alone to discover correlations.

Use a dedicated analysis layer:

```text
Raw health data
       ↓
Normalize
       ↓
Align timestamps
       ↓
Daily/appropriate aggregation
       ↓
Personal baseline calculation
       ↓
Pattern / correlation analysis
       ↓
Evidence + confidence
       ↓
Rank insights
       ↓
LLM explanation
       ↓
UI + charts
```

The deterministic/data-analysis layer determines:

> What relationship does the recorded data support?

The LLM determines:

> How should this finding be explained to the user?

---

# 3. Weekly Correlation Analysis

The primary initial use case is the Weekly Health Review.

For a selected week:

1. Retrieve all relevant data.
2. Normalize units.
3. Normalize timestamps/timezone.
4. Convert appropriate metrics into daily observations.
5. Calculate personal baselines.
6. Identify deviations.
7. Analyze predefined health-relevant relationships.
8. Calculate evidence strength.
9. Rank findings.
10. Send only supported findings to the AI.
11. Generate concise user-facing explanations.
12. Attach relevant supporting charts/data.

---

# 4. Time Alignment

Use comparable time periods.

Example:

```text
Date       Caffeine    Sleep    Steps    Water    Headache
Mon        1 serving   7.4h     8200     2.4L     Yes
Tue        4 servings  5.9h     5400     1.5L     No
Wed        3 servings  6.2h     6100     1.4L     Yes
Thu        1 serving   7.6h     9100     2.3L     No
```

This enables cross-metric analysis.

Use the user's local timezone.

Do not mix UTC dates with local dates.

---

# 5. Personal Baselines

Prefer personal baselines over generic thresholds.

Potential baselines:

1. Previous week
2. 4-week average
3. Longer historical baseline where sufficient data exists

Example:

```text
Typical caffeine:
1.5 servings/day

This week:
3.2 servings/day

Typical sleep:
7h 20m

This week:
6h 25m
```

This is more useful than simply deciding:

> 3 coffees = high.

Personalized analysis should be the default whenever enough historical data exists.

---

# 6. Supported Relationship Registry

Do not blindly calculate every possible pair of metrics.

Maintain a curated registry of clinically/relevantly meaningful relationships.

## Sleep

Analyze sleep against:

- Caffeine
- Late caffeine
- Alcohol
- Exercise
- Steps/activity
- Stress
- Mood
- Hydration
- Symptoms

## Headache

Analyze headache occurrence against:

- Sleep duration
- Sleep quality
- Hydration
- Caffeine
- Stress
- Activity

## Fatigue

Analyze fatigue against:

- Sleep
- Activity
- Stress
- Recovery
- HRV

## Resting heart rate

Analyze against:

- Sleep
- Stress
- Activity
- Exercise
- Alcohol
- Recovery

## HRV

Analyze against:

- Sleep
- Stress
- Exercise
- Activity
- Alcohol
- Recovery

## Recovery/readiness

Analyze against:

- Sleep
- Exercise
- Activity
- Stress
- HRV
- Resting heart rate

## Stress

Analyze against:

- Sleep
- Activity
- Exercise
- Resting heart rate
- HRV

Extend the registry as new metrics become available.

---

# 7. Caffeine → Sleep

Analyze relationships between:

- Caffeine amount
- Caffeine timing
- Sleep duration
- Sleep quality
- Sleep latency

Examples:

> Higher caffeine intake coincided with shorter sleep.

> Sleep quality was lower on days when caffeine was consumed later in the day.

If caffeine timing exists, prefer timing-aware analysis.

Never automatically say:

> Caffeine caused poor sleep.

---

# 8. Sleep → Fatigue

If fatigue is reported:

Analyze whether fatigue occurs after:

- Shorter sleep
- Poor sleep quality
- Irregular sleep

Example:

> Both fatigue reports occurred after nights with below-average sleep.

---

# 9. Sleep → Headache

Analyze:

- Sleep duration
- Sleep quality
- Sleep consistency

against headache occurrences.

Example:

> Headaches were reported on three days, and all three followed nights with shorter-than-usual sleep.

Do not claim causation.

---

# 10. Hydration → Headache

Analyze:

- Water intake
- Hydration target achievement

against headache occurrences.

Example:

> Two of your three headache days also had lower-than-usual water intake.

---

# 11. Exercise → Sleep

Analyze whether exercise is associated with:

- Sleep duration
- Sleep quality
- Sleep latency
- Recovery

Where appropriate, analyze same-day and next-night effects.

Example:

> Your sleep duration tended to be higher following moderate-exercise days.

---

# 12. Activity → Sleep

Analyze:

- Steps
- Active minutes
- Distance
- Sedentary time

against sleep.

Example:

> Your higher-activity days were generally followed by longer sleep.

---

# 13. Stress → Sleep

Analyze:

- Stress score
- Reported stress
- High-stress days

against:

- Sleep duration
- Sleep quality
- Sleep latency

Example:

> Your highest-stress days were also your shortest-sleep nights.

---

# 14. Stress → Heart Rate / HRV

Where sufficient data exists:

Analyze:

```text
Stress → resting HR
Stress → HRV
```

Example:

> Resting heart rate was slightly higher on your highest-stress days.

or:

> HRV tended to be lower on your higher-stress days.

Do not overinterpret wearable metrics.

---

# 15. Alcohol → Sleep / Recovery

Where alcohol data exists:

Analyze:

- Sleep quality
- Sleep duration
- Resting HR
- HRV
- Recovery

Example:

> Sleep quality was lower on the nights following alcohol intake.

Use cautious language.

---

# 16. Medication Relationships

Medication changes may be analyzed only when appropriate structured data exists.

Potential relationships:

```text
Medication started/changed
          ↓
New symptom
```

or:

```text
Medication timing
      ↓
Reported symptom
```

Example:

> Nausea was first reported after your medication was changed.

Do NOT automatically say:

> The medication caused nausea.

Do not infer medication side effects solely from temporal correlation.

---

# 17. Same-Day vs Lagged Relationships

Relationships must specify the temporal window.

Possible windows:

- Same day
- Same night
- Next day
- Previous night → next day
- Other explicitly defined lag

Examples:

```text
Caffeine → same-night sleep

Exercise → following-night sleep

Sleep → next-day fatigue

Alcohol → same-night sleep

Sleep → next-day recovery
```

Do not randomly test many lag periods and select whichever produces the strongest relationship without accounting for multiple comparisons.

---

# 18. Statistical Methods

When enough observations exist, use appropriate methods.

Potential methods:

- Pearson correlation
- Spearman correlation
- Point-biserial correlation
- Difference in means/medians
- Event-day vs non-event-day comparisons
- Lagged correlation

Choose the method appropriate to the data type.

Do not use statistical significance alone to determine whether an insight is meaningful.

Consider:

- Sample size
- Effect size
- Consistency
- Direction
- Personal baseline
- Data quality
- Practical relevance

---

# 19. Minimum Evidence

Do not generate a meaningful correlation from one isolated event.

As a default:

- Prefer at least 3 comparable observations for a repeated pattern.
- Use more observations when performing statistical correlation.
- With insufficient observations, report the event itself rather than a correlation.

Example with insufficient evidence:

> You had high caffeine intake and poor sleep on Tuesday.

Do not say:

> High caffeine is associated with poor sleep.

Example with repeated evidence:

> Your higher-caffeine days tended to coincide with shorter sleep this week.

---

# 20. Correlation Strength

Each finding should have internal evidence metadata.

Suggested levels:

### Strong

Consistent relationship across multiple observations with sufficient data.

### Moderate

Repeated relationship with some variability.

### Weak

Possible pattern but insufficient consistency.

### Insufficient

Not enough evidence to surface as a correlation.

Only surface appropriate findings to the user.

---

# 21. Confounding Factors

Avoid simplistic interpretations.

Example:

```text
High caffeine
      ↓
Poor sleep
```

could actually involve:

```text
High stress
    ↓
Poor sleep
    ↓
More caffeine
```

When data is available, consider other relevant variables:

- Stress
- Activity
- Exercise
- Alcohol
- Sleep
- Symptoms
- Medication changes

Example output:

> Higher caffeine intake coincided with poorer sleep. Stress was also higher on those days, so the recorded data does not establish which factor contributed most.

---

# 22. Do Not Create Correlation Explosion

If there are 30 metrics, there are hundreds of possible pairs.

Do NOT analyze everything blindly.

Use the curated relationship registry.

Only add additional relationships when there is a clear health/product reason.

---

# 23. Insight Ranking

Rank findings using:

1. Evidence strength
2. Number of supporting observations
3. Relevance to reported symptoms
4. Magnitude of deviation
5. Consistency
6. Personal baseline relevance
7. Potential usefulness to the user

Prioritize findings involving symptoms or meaningful health changes.

---

# 24. Limit User-Facing Insights

Do not overwhelm the user.

Recommended:

**2–5 meaningful cross-metric insights per weekly review.**

If 20 relationships are detected, rank them and show only the most useful ones.

Additional details can be available through an expandable section if the UI supports it.

---

# 25. Structured Correlation Result

Represent findings structurally before sending them to the AI.

Example:

```json
{
  "type": "cross_metric_association",
  "metricA": "caffeine",
  "metricB": "sleep_duration",
  "direction": "negative",
  "temporalWindow": "same_night",
  "observations": 7,
  "supportingObservations": 4,
  "strength": "moderate",
  "confidence": "moderate",
  "baselineAware": true,
  "causalityEstablished": false,
  "summary": "Higher caffeine intake coincided with shorter sleep."
}
```

Symptom example:

```json
{
  "type": "symptom_association",
  "symptom": "headache",
  "metric": "sleep_duration",
  "direction": "negative",
  "observations": 7,
  "symptomOccurrences": 3,
  "supportingOccurrences": 3,
  "strength": "moderate",
  "confidence": "moderate",
  "baselineAware": true,
  "causalityEstablished": false,
  "summary": "Headaches occurred on days following shorter sleep."
}
```

Adapt to existing project DTO conventions.

---

# 26. AI Input

The AI should receive computed findings, not raw unprocessed data alone.

For each finding, provide:

- Variables
- Time window
- Observation count
- Supporting observation count
- Direction
- Strength
- Confidence
- Baseline comparison
- Relevant supporting data
- Confounding factors where identified
- Causality status

Example:

```text
Finding:
Caffeine and sleep duration show a moderate negative association.

Observations:
7 days.

Higher-than-baseline caffeine:
4 days.

Below-baseline sleep:
4 days.

Overlap:
4 days.

Stress:
Also elevated on 2 of those days.

Causality:
Not established.
```

The AI should convert this into concise, cautious language.

---

# 27. AI Language Rules

Prefer:

- "coincided with"
- "occurred alongside"
- "was associated with"
- "tended to"
- "your data suggests"
- "may be contributing"
- "you may want to observe whether..."

Avoid:

- "caused"
- "is the reason"
- "proves"
- "definitely caused"
- "this confirms"
- "this means you have..."

unless causality is independently established by appropriate evidence.

---

# 28. User-Facing Correlation Examples

### Caffeine / sleep

> ☕ **Caffeine & sleep:** Your higher-caffeine days tended to coincide with shorter sleep.

### Sleep / fatigue

> 😴 **Sleep & fatigue:** Both fatigue reports followed nights when you slept less than usual.

### Hydration / headache

> 💧 **Hydration & headaches:** Two of your three headache days also had lower-than-usual water intake.

### Stress / sleep

> 🧠 **Stress & sleep:** Your highest-stress days were also your shortest-sleep nights.

Always make it clear that these are observations rather than proof of causation.

---

# 29. Supporting Charts

When useful, show supporting visualizations.

For example:

### Caffeine vs sleep

A chart can show daily caffeine intake alongside sleep duration.

### Activity vs sleep

A chart can show daily steps and subsequent sleep.

### Symptom relationship

A chart can show the metric trend with symptom occurrence markers if the existing chart infrastructure supports this.

Do not create a chart for every correlation.

The chart should materially improve understanding.

Reuse existing chart components.

Do not create a second charting framework.

---

# 30. Weekly Review Integration

The correlation engine should feed the Weekly Health Review.

Example architecture:

```text
Weekly Health Review
        ↓
Fetch weekly data
        ↓
Normalize
        ↓
Aggregate
        ↓
Calculate baselines
        ↓
Run correlation/pattern analysis
        ↓
Rank findings
        ↓
Generate AI narrative
        ↓
Return:
    - Overall summary
    - Symptoms
    - Metrics
    - Trends
    - Correlations
    - Takeaways
    - Charts
```

The correlation engine should be independently testable and reusable.

---

# 31. Missing Data

Never treat missing data as zero.

Example:

If sleep exists for 7 days but caffeine only exists for 3 days:

Do not calculate a 7-day caffeine/sleep correlation as though caffeine were zero for the missing days.

Instead:

> Caffeine data was available for 3 of 7 days.

Only perform the analysis if sufficient overlapping observations exist.

---

# 32. Data Quality

Before analysis:

- Validate timestamps
- Normalize timezone
- Normalize units
- Remove duplicates where appropriate
- Handle missing values
- Verify data coverage
- Avoid double-counting multiple wearable sources
- Respect existing source-priority rules
- Preserve measurement precision

Do not silently repair questionable data without following existing application rules.

---

# 33. Safety

This is a health-related analytics feature.

The system must:

- Never fabricate measurements.
- Never fabricate symptoms.
- Never fabricate correlations.
- Never diagnose from correlations.
- Never claim causality from observational data.
- Never imply that wearable data alone establishes disease.
- Clearly communicate uncertainty.
- Avoid unnecessary alarm.
- Escalate/flag appropriately when the actual underlying health data warrants attention.

The correlation engine should produce **evidence-backed observations**, not medical diagnoses.

---

# 34. Testing

Add tests for:

### Basic correlation

- Correct daily alignment
- Correct aggregation
- Correct baseline calculation

### Caffeine / sleep

- Positive/negative patterns detected correctly
- Insufficient observations rejected
- Personal baseline correctly applied

### Symptoms

- Symptom dates align correctly with health metrics
- Repeated symptoms are handled correctly
- Symptom correlation does not become diagnosis

### Lagged relationships

- Same-day relationships work
- Next-day relationships work
- Previous-night → next-day relationships work

### Missing data

- Missing data is not treated as zero
- Insufficient overlapping observations prevent correlation

### Confounding

- Additional relevant metrics can be considered
- AI receives uncertainty/confounding information

### Ranking

- Stronger/more relevant findings rank above weak findings
- Maximum user-facing insights is respected

### AI

- AI receives computed evidence
- AI cannot invent correlations
- Causal language is prevented/validated where possible
- Structured output is validated

### Regression

Run the existing relevant test suite.

---

# 35. Definition of Done

The correlation feature is complete when:

- Weekly health data can be analyzed across multiple dimensions.
- Personal baselines are used where sufficient data exists.
- Symptoms can be correlated with relevant metrics.
- Lifestyle metrics can be correlated with sleep/recovery.
- Wearable metrics can be correlated with each other where meaningful.
- Same-day and appropriate lagged relationships are supported.
- Correlations require sufficient evidence.
- Missing data is handled correctly.
- Confounding factors are considered where possible.
- Findings are ranked.
- Only the most useful findings reach the user.
- The AI explains computed findings instead of inventing them.
- Causality is never implied without appropriate evidence.
- Supporting charts can be displayed alongside insights.
- Existing chart/AI infrastructure is reused.
- Tests cover analysis, safety, missing data, ranking, and AI output.

## Core principle

The system should answer:

> **“What things in my health data appear to move together, and what might be worth paying attention to?”**

It should identify patterns without pretending that observational health data proves causation.

The analytics layer establishes the evidence.

The AI explains the evidence.

The UI presents the evidence and supporting data clearly.