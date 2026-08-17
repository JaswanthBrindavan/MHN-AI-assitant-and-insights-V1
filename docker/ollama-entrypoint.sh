#!/bin/sh
# Entrypoint for the Davi embeddings service (Dockerfile.ollama).
#
# Serve FIRST so Railway's healthcheck (GET / -> 200) passes immediately,
# then ensure the embedding model is actually present. The model is baked
# into the image at /bundled-models, but if anything hides or drops that
# layer at runtime (an attached volume shadowing the path, a platform image
# quirk), this pulls it once and the service self-heals. With a volume
# mounted at $OLLAMA_MODELS the pull persists across restarts.

MODEL="${EMBED_MODEL:-qwen3-embedding:0.6b}"

ollama serve &
SERVE_PID=$!

sleep 3
if ollama show "$MODEL" >/dev/null 2>&1; then
    echo "entrypoint: model $MODEL present (baked layer intact)"
else
    echo "entrypoint: model $MODEL MISSING at runtime — pulling (one-time)"
    until ollama pull "$MODEL"; do
        echo "entrypoint: pull failed; retrying in 10s"
        sleep 10
    done
    echo "entrypoint: model $MODEL ready"
fi

# Keep the container's lifetime tied to the server.
wait $SERVE_PID
