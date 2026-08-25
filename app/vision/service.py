"""Vision — reading an image the reader is entitled to.

Two things make this safe rather than reckless:

**It is gated by the same consent as every other family read.** Vision is only
ever reached through ``documents.fetch``, which refuses before any network call
if the reader is not entitled to the file. There is no path from a chat message
to an image the four-condition gate would deny.

**Its output is UNTRUSTED TEXT.** A vision model describing a photo is a
generator like any other: what it returns goes through ``validate_reply``, the
numeric-fidelity guard and the grounding verifier exactly as a text answer does.
A model that reads "Metformin 500mg" off a pill strip has produced a claim, not
a fact, and it is treated as one.

The prompts here are deliberately narrow. Each says what the model may report
and what it must refuse to conclude, because the failure mode is not a model
that says nothing — it is a model that confidently identifies a rash, a pill, or
a diagnosis from a photograph.

DRAFT — like every clinical constant in this repo, these prompts need clinician
sign-off before non-synthetic use.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from app.config import get_settings
from app.documents.fetch import FetchedDocument

logger = logging.getLogger("davi.vision")

# What the reader might plausibly photograph, and what each is allowed to
# produce. Anything not listed falls back to `document`.
KINDS = ("document", "medicine", "skin", "unknown")

_SHARED_RULES = (
    "You are reading an image for a health assistant that offers decision "
    "support and NEVER diagnoses.\n"
    "- Describe only what is legibly visible. Do not infer, guess, or fill "
    "gaps from what is typical.\n"
    "- If the image is unclear, partly cut off, or you are unsure, say so "
    "plainly. 'I can't read that clearly' is a correct and useful answer.\n"
    "- Never state or imply that the reader has a condition.\n"
    "- Never tell the reader to start, stop or change a medication.\n"
    "- Do not invent numbers. Report a value only if you can actually read it."
)

_PROMPTS = {
    "document": (
        _SHARED_RULES
        + "\n\nThis is a photographed health document. Transcribe the test "
        "names and values you can clearly read, with their units, and note "
        "anything the report itself flags as out of range. Do not interpret "
        "what the results mean for the reader — another part of the system "
        "does that from validated content."
    ),
    "medicine": (
        _SHARED_RULES
        + "\n\nThis is a photograph of a medicine pack or strip. Read the "
        "printed brand name, the composition and the strength EXACTLY as "
        "printed. Do not identify a medicine from the appearance of a loose "
        "tablet — colour and shape are not identification, and getting this "
        "wrong is dangerous. If the packaging is not legible, say so."
    ),
    "skin": (
        _SHARED_RULES
        + "\n\nThis is a photograph of a skin, eye or visible-body concern. "
        "Describe only the visible characteristics — location, colour, "
        "distribution, whether it looks raised or flat. Do NOT name a "
        "condition and do not offer a likely cause. A photograph cannot "
        "diagnose, and a confident-sounding guess here would be actively "
        "harmful. Close by noting that a clinician needs to look at it "
        "properly."
    ),
    "unknown": (
        _SHARED_RULES
        + "\n\nDescribe what this image shows, in one or two sentences, and "
        "whether it looks health-related at all."
    ),
}


@dataclass(frozen=True)
class VisionResult:
    """What a vision model reported. Untrusted — treat it as generated text."""

    text: str
    kind: str
    model_name: str

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def vision_enabled() -> bool:
    settings = get_settings()
    return bool(settings.vision_enabled and settings.vision_model)


def prompt_for(kind: str) -> str:
    return _PROMPTS.get(kind, _PROMPTS["unknown"])


def to_image_block(doc: FetchedDocument) -> dict:
    """The provider-neutral image block, mirroring app/llm/tools.py's approach.

    Base64 rather than a URL: the presigned link is short-lived and pointing a
    third party at it would hand out access this service was careful to keep
    scoped.
    """
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": doc.content_type,
            "data": base64.standard_b64encode(doc.content).decode("ascii"),
        },
    }


async def describe_image(
    provider,
    doc: FetchedDocument,
    kind: str = "document",
    question: str = "",
) -> VisionResult | None:
    """Ask the model to read an image. None on any failure.

    Never raises: vision is an enhancement, and a failure must degrade to the
    extracted ``content.ai`` the chat has always used rather than cost a reply.
    """
    if not vision_enabled():
        return None
    if not doc.content:
        return None

    user_text = question.strip() or "What does this image show?"
    try:
        turn = await provider.generate_turn(
            system=prompt_for(kind),
            messages=[_image_message(doc, user_text)],
            tools=(),
        )
    except Exception:  # noqa: BLE001 — vision must never break a reply
        logger.warning("vision request failed", exc_info=True)
        return None

    text = (turn.text or "").strip()
    if not text:
        return None
    return VisionResult(
        text=text, kind=kind, model_name=getattr(provider, "model_name", "vision")
    )


def _image_message(doc: FetchedDocument, text: str):
    """A UserMessage carrying an image.

    The internal vocabulary is text-only by design, so the image travels as a
    structured attachment the adapters translate. Keeping it out of `content`
    means nothing upstream has to know an image is involved.
    """
    from app.llm.tools import UserMessage

    return UserMessage(content=text, attachments=(to_image_block(doc),))
