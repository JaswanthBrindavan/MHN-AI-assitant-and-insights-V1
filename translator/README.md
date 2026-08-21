# Davi translation sidecar

Self-hosted English-pivot translation for Indian languages. All models are
open-source and run **inside your infrastructure** — chat text never goes to
a third-party translation vendor.

| Job | Model | License |
|---|---|---|
| En ↔ Indic translation | AI4Bharat IndicTrans2 dist-200M (both directions) | MIT |
| Romanized → native script (and back) | AI4Bharat IndicXlit | MIT |
| Romanized language ID ("naaku noppi undi" → Telugu) | AI4Bharat IndicLID-FTR | MIT |

Languages: Hindi, Bengali, Punjabi, Gujarati, Odia, Tamil, Telugu, Kannada,
Malayalam, Marathi — native script and romanized (Latin) typing.

## API

```
GET  /health                  → {"status":"ok","ready":true}
POST /detect    {"text": "..."}
                → {"language":"te","script":"latin","confidence":0.93}
POST /translate {"text":"...","language":"te",
                 "direction":"to_english"|"from_english",
                 "script":"native"|"latin"}
                → {"text":"..."}
```

Set `TRANSLATOR_TOKEN` to require `Authorization: Bearer <token>` on both
POST endpoints (recommended even on private networking).

## Deploy (Railway)

1. New service from this repo, **root directory `translator/`** (the
   Dockerfile is picked up automatically; models are baked in at build —
   image is ~3 GB, first build takes a while).
2. Optionally set `TRANSLATOR_TOKEN`.
3. On the Davi service set:
   - `TRANSLATE_BASE_URL=http://<service-name>.railway.internal:8000`
     (private networking is http, and the scheme is required)
   - `TRANSLATE_TOKEN=<same token>` if you set one.

Unset `TRANSLATE_BASE_URL` turns the feature off — Davi then answers in
English with the reply-language directive on the LLM (fail-open, same as
when this service is down).

## Local run

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install cython numpy && pip install -r requirements.txt
uvicorn app:app --port 8000
```

First start downloads ~1.5 GB of models to the HF cache. CPU is fine:
dist-200M translates a chat sentence in well under a second.
