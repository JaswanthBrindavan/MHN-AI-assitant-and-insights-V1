"""Self-hosted translation sidecar for Davi (AI4Bharat stack, CPU).

Models (all self-hosted — PHI never leaves the deployment):
  * IndicTrans2 dist-200M (MIT), both directions — En↔Indic translation
  * IndicXlit (MIT) — roman↔native transliteration for romanized typing
  * IndicLID-FTR (MIT, fasttext) — romanized-Indic language identification

Endpoints (mirrored by app/translate/service.py in the main backend):
  POST /detect    {"text"}                       → {"language","script","confidence"}
  POST /translate {"text","language","direction","script"} → {"text"}
  GET  /health                                   → {"status","ready"}

Deliberately tiny: one process, models loaded once at startup, no queue.
"""

from __future__ import annotations

import os
import re
import threading

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# Davi language code → IndicTrans2 FLORES tag. IndicXlit uses the same
# two-letter codes we do.
LANGS = {
    "hi": "hin_Deva", "bn": "ben_Beng", "pa": "pan_Guru", "gu": "guj_Gujr",
    "or": "ory_Orya", "ta": "tam_Taml", "te": "tel_Telu", "kn": "kan_Knda",
    "ml": "mal_Mlym", "mr": "mar_Deva",
}
# IndicLID label prefix (ISO-639-3) → Davi code.
_ISO3 = {
    "hin": "hi", "ben": "bn", "pan": "pa", "guj": "gu", "ori": "or",
    "ory": "or", "tam": "ta", "tel": "te", "kan": "kn", "mal": "ml",
    "mar": "mr", "eng": "en",
}
_SCRIPT_RANGES = {
    "hi": range(0x0900, 0x0980), "bn": range(0x0980, 0x0A00),
    "pa": range(0x0A00, 0x0A80), "gu": range(0x0A80, 0x0B00),
    "or": range(0x0B00, 0x0B80), "ta": range(0x0B80, 0x0C00),
    "te": range(0x0C00, 0x0C80), "kn": range(0x0C80, 0x0D00),
    "ml": range(0x0D00, 0x0D80),
}
_SENTENCES = re.compile(r"(?<=[.!?।])\s+")
TOKEN = os.environ.get("TRANSLATOR_TOKEN", "")


class _Engine:
    """All four models, loaded once. Heavy imports stay inside so the module
    imports instantly for tests."""

    def __init__(self) -> None:
        import fasttext
        import torch
        from ai4bharat.transliteration import XlitEngine
        from huggingface_hub import hf_hub_download
        from IndicTransToolkit.processor import IndicProcessor
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        self.torch = torch
        self.ip = IndicProcessor(inference=True)

        def load(repo: str):
            tok = AutoTokenizer.from_pretrained(repo, trust_remote_code=True)
            model = AutoModelForSeq2SeqLM.from_pretrained(
                repo, trust_remote_code=True
            )
            model.eval()
            return tok, model

        self.to_en = load("ai4bharat/indictrans2-indic-en-dist-200M")
        self.from_en = load("ai4bharat/indictrans2-en-indic-dist-200M")
        self.lid = fasttext.load_model(
            hf_hub_download("ai4bharat/IndicLID-FTR", "model_baseline_roman.bin")
        )
        self.xlit_to_native = XlitEngine(
            list(LANGS), beam_width=4, rescore=False, src_script_type="roman"
        )
        self.xlit_to_roman = XlitEngine(
            beam_width=4, rescore=False, src_script_type="indic"
        )

    # -- helpers ---------------------------------------------------------- #
    def _xlit(self, engine, text: str, lang: str) -> str:
        out = engine.translit_sentence(text, lang_code=lang)
        # The package returns {"hi": "..."} for roman→native and a plain
        # string for indic→roman; normalize both.
        return out.get(lang, text) if isinstance(out, dict) else out

    def _mt(self, sents: list[str], src: str, tgt: str, pair) -> list[str]:
        tok, model = pair
        batch = self.ip.preprocess_batch(sents, src_lang=src, tgt_lang=tgt)
        inputs = tok(
            batch, truncation=True, padding="longest",
            return_tensors="pt", max_length=256,
        )
        with self.torch.no_grad():
            gen = model.generate(
                **inputs, max_length=256, num_beams=5, num_return_sequences=1
            )
        dec = tok.batch_decode(
            gen, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        return self.ip.postprocess_batch(dec, lang=tgt)

    def _translate_block(self, text: str, src: str, tgt: str, pair) -> str:
        # Line-by-line keeps list/paragraph structure; sentences batch per
        # line. ponytail: no markdown-aware masking — the caller's
        # digit-fidelity check and English fail-open cover mangled output.
        out_lines: list[str] = []
        for line in text.split("\n"):
            core = line.strip()
            if not core:
                out_lines.append(line)
                continue
            sents = [s for s in _SENTENCES.split(core) if s.strip()]
            out_lines.append(" ".join(self._mt(sents, src, tgt, pair)))
        return "\n".join(out_lines)

    # -- API operations --------------------------------------------------- #
    def detect(self, text: str) -> dict:
        counts: dict[str, int] = {}
        for ch in text:
            cp = ord(ch)
            for lang, rng in _SCRIPT_RANGES.items():
                if cp in rng:
                    counts[lang] = counts.get(lang, 0) + 1
                    break
        if counts:
            best = max(counts.items(), key=lambda kv: kv[1])
            if best[1] >= 4:
                lang = best[0]
                if lang == "hi":  # Devanagari: hi or mr — ask the LID model
                    labels, scores = self.lid.predict(text.replace("\n", " "))
                    got = _ISO3.get(labels[0].removeprefix("__label__")[:3])
                    if got == "mr":
                        lang = "mr"
                return {"language": lang, "script": "native", "confidence": 1.0}
        labels, scores = self.lid.predict(text.replace("\n", " "))
        lang = _ISO3.get(labels[0].removeprefix("__label__")[:3], "en")
        return {
            "language": lang, "script": "latin",
            "confidence": round(float(scores[0]), 4),
        }

    def translate(self, text: str, language: str, direction: str, script: str) -> str:
        flores = LANGS[language]
        if direction == "to_english":
            if script == "latin":
                text = self._xlit(self.xlit_to_native, text, language)
            return self._translate_block(text, flores, "eng_Latn", self.to_en)
        out = self._translate_block(text, "eng_Latn", flores, self.from_en)
        if script == "latin":
            out = self._xlit(self.xlit_to_roman, out, language)
        return out


_engine: _Engine | None = None
_lock = threading.Lock()


def get_engine() -> _Engine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = _Engine()
        return _engine


app = FastAPI(title="davi-translator")


def _auth(authorization: str | None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


class DetectIn(BaseModel):
    text: str


class TranslateIn(BaseModel):
    text: str
    language: str
    direction: str  # "to_english" | "from_english"
    script: str  # "native" | "latin"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "ready": _engine is not None}


@app.post("/detect")
def detect(body: DetectIn, authorization: str | None = Header(None)) -> dict:
    _auth(authorization)
    return get_engine().detect(body.text)


@app.post("/translate")
def translate(body: TranslateIn, authorization: str | None = Header(None)) -> dict:
    _auth(authorization)
    if body.language not in LANGS:
        raise HTTPException(status_code=400, detail="unsupported language")
    if body.direction not in {"to_english", "from_english"}:
        raise HTTPException(status_code=400, detail="bad direction")
    return {
        "text": get_engine().translate(
            body.text, body.language, body.direction, body.script
        )
    }


@app.on_event("startup")
def warm() -> None:
    # Load everything up front so the first user request isn't the slow one.
    threading.Thread(target=get_engine, daemon=True).start()
