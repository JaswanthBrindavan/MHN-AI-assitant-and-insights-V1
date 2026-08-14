# Manual smoke test — Ollama provider

The automated test suite never touches a live LLM. To exercise the real
`OllamaProvider` end-to-end, run Ollama locally and point the API at it.

## 1. Start Ollama

```bash
ollama serve            # exposes the OpenAI-compatible API at http://localhost:11434/v1
ollama pull llama3.1    # or any chat model you prefer
```

## 2. Configure and run the API

```bash
export LLM_PROVIDER=ollama
export OLLAMA_BASE_URL=http://localhost:11434/v1
export OLLAMA_MODEL=llama3.1
export GROUNDING_MODE=enforce      # off | log | enforce
export AUTH_ENABLED=false

alembic upgrade head
python -m scripts.seed_synthetic
python -m scripts.ingest_knowledge knowledge
uvicorn app.main:app --reload
```

## 3. Send a chat turn

```bash
curl -s http://localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: 11111111-1111-1111-1111-111111111111' \
  -d '{"message":"what should I know about type 2 diabetes in my family?"}' | jq
```

Expect a JSON body with `response_message` (citation markers stripped),
`risk_level`, `recommended_action`, `provenance`, and `grounding`. Try an
emergency phrase (`"I can't breathe"`) to see the deterministic directive, and a
red-flag co-occurrence (`"chest pain and sweating"`) to see the ACS escalation.

## What to look for

- Markers (`[1]`, `[P]`, `[GK]`) never appear in `response_message`.
- With `GROUNDING_MODE=enforce`, an ungrounded numeric claim triggers exactly one
  corrective retry; persistent failure degrades to the safe reply.
- A `rag_turn_receipts` row is written per turn, storing only the query **hash**.
