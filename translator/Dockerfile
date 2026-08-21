# Davi translation sidecar — CPU-only, models baked into the image so the
# container starts ready (no cold-start downloads, no volume needed).
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# CPU torch first (the plain PyPI wheel drags in CUDA).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir cython numpy \
 && pip install --no-cache-dir -r requirements.txt

# Bake every model into the image at build time.
RUN python - <<'EOF'
from huggingface_hub import snapshot_download, hf_hub_download
snapshot_download("ai4bharat/indictrans2-indic-en-dist-200M")
snapshot_download("ai4bharat/indictrans2-en-indic-dist-200M")
hf_hub_download("ai4bharat/IndicLID-FTR", "model_baseline_roman.bin")
from ai4bharat.transliteration import XlitEngine
XlitEngine(["hi", "te"], beam_width=4, rescore=False, src_script_type="roman")
XlitEngine(beam_width=4, rescore=False, src_script_type="indic")
EOF

COPY app.py .
ENV PORT=8000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
