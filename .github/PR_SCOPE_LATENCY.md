# Off-topic lookups were costing 30 seconds and a model call

Both changes came out of running the chat live against staging after the
`LLM_MODEL` fix. Measured, not inferred.

---

## 1. The scope guard did not cover real-time lookups

*"what is the weather in Hyderabad today?"* fell through to the RAG path and
spent **29.8 seconds** on a full LLM round trip — to say it could not answer.

The reply was correct. The route was wrong. The reader waited half a minute for
a refusal we could have given in 100ms, and we paid for the tokens.

`scope.py` is a narrow allowlist of code / math / trivia patterns, so weather
was simply never covered. Added: weather, news, stock and crypto prices,
directions and traffic, and "what time is it".

### The trap, and why every pattern matches request *shape*

**"weather", "temperature", "time", "traffic", "news" and "price" are all
ordinary health vocabulary.** A naive `\bweather\b` would decline a genuine
asthma question. So the guard matches how the request is *phrased* — asking
*for* a forecast reads differently from asking what weather does to a body —
and never the topic word alone.

Nine of the 21 new tests exist purely to protect that boundary:

```
does cold weather make my asthma worse?
my joints ache when the weather changes
is it normal to feel dizzy in hot weather?
my temperature is 39 degrees
what time should I take my metformin?
what time of day is blood pressure highest?
I get breathless in traffic fumes
what is the price of my insulin without insurance?
the news about my diagnosis has been stressful
```

Every one must still reach the model, and each is now a failing test if the
guard gets greedier. **29.8s → ~0.1s, zero tokens.**

---

## 2. The trace said what happened, never where the time went

Every trace step now carries `ms` since the turn began. The agentic path takes
`t` as a parameter, so it inherits this for free.

This is **instrumentation, not an optimisation**, and that is deliberate. Here
is the staging measurement that motivated it:

| Turn | Wall | Output tok | Generation at ~56 tok/s | Unexplained |
|---|---:|---:|---:|---:|
| T2DM symptoms | 10.4s | 581 | ~10.4s | **~0s** |
| honey + lemon | 19.0s | 224 | ~4.0s | **~15s** |
| medications | 22.5s | 203 | ~3.6s | **~19s** |
| weather | 29.8s | 222 | ~4.0s | **~26s** |

The fast turn is *pure generation* — so generation is not the bottleneck. The
slow ones all failed to match a condition, which points at the unscoped
retrieval fallback (`ILIKE '%token%'` OR'd eight ways, leading wildcard, no
usable index).

**But the arithmetic does not actually support that.** A full scan of a few
thousand chunks is milliseconds, not twenty seconds. It is a hypothesis, and
shipping an optimisation against an unproven one is how you fix the wrong thing
convincingly. One live turn with these timings will name the real stage.

---

## Not in this PR — the lever that actually gets us under 15s

`/chat/stream` already exists and works here (`app/api/v1/chat.py:267`,
`AnthropicProvider.generate_stream`). **The frontend POSTs to `/chat` and waits
for the whole reply.** Switching mhn-react to the streaming endpoint puts first
token on screen in 1–2s whatever total generation costs — no backend change
needed.

Also standing: **nothing caches today.** The legacy prefix is 267 tokens, under
Sonnet's 1,024 minimum, so every turn pays full prefill. That needs the agentic
engine (Task 12) or the second breakpoint (D1).

---

## Verification

```
1,887 passed  (21 new scope cases)
ruff clean · pyright 0 errors
run_evals 17/17 on BOTH engines
```

Also verified live in staging this session, on the deployed build: the three
V18 lab-value answers (LDL / HDL / haemoglobin) now match the right parameter
in the right units; the drug path reads `medicine_master` with `used_for` and
`habit_forming` rendering correctly and no truncated words; the interaction
refusal fires deterministically in 407ms; and the emergency floor fires in
163ms — even when the same message also carried a model-identity question.
