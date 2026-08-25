"""Vision — gated by consent, and its output treated as untrusted text.

The two properties worth pinning:
  * a vision model's description goes through exactly the same output guards as
    any other generated text — it is a claim, not a fact
  * the prompts refuse the dangerous conclusions rather than merely omitting
    them, because the failure mode is a model that confidently identifies a
    rash or a loose tablet from a photograph
"""

from __future__ import annotations

import base64

import pytest

from app.documents.fetch import FetchedDocument
from app.llm.anthropic import _to_anthropic_messages
from app.llm.fake import FakeProvider
from app.llm.openai_compat import _to_openai_messages
from app.llm.tools import LLMTurn, UserMessage
from app.vision.service import (
    KINDS,
    describe_image,
    prompt_for,
    to_image_block,
    vision_enabled,
)

DOC = FetchedDocument(
    content=b"\xff\xd8\xffimagebytes",
    content_type="image/jpeg",
    resource_type="reports",
    resource_id=1,
)


@pytest.fixture
def vision_on(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("VISION_ENABLED", "true")
    monkeypatch.setenv("VISION_MODEL", "some-vision-model")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The feature flag
# --------------------------------------------------------------------------- #
def test_vision_is_off_by_default():
    assert not vision_enabled()


async def test_nothing_happens_when_vision_is_off():
    provider = FakeProvider(turns=[LLMTurn(text="should not be reached")])
    assert await describe_image(provider, DOC) is None
    assert provider.calls == []


async def test_vision_runs_when_enabled(vision_on):
    provider = FakeProvider(turns=[LLMTurn(text="A lab report showing HbA1c 6.1%.")])
    result = await describe_image(provider, DOC, kind="document")
    assert result is not None
    assert result.usable
    assert "6.1%" in result.text


# --------------------------------------------------------------------------- #
# The prompts refuse, rather than merely omit
# --------------------------------------------------------------------------- #
def test_every_kind_has_a_prompt():
    for kind in KINDS:
        assert prompt_for(kind).strip()


def test_an_unknown_kind_falls_back_rather_than_failing():
    assert prompt_for("something-new") == prompt_for("unknown")


def test_every_prompt_forbids_diagnosing():
    for kind in KINDS:
        assert "never diagnoses" in prompt_for(kind).lower()


def test_every_prompt_forbids_medication_changes():
    for kind in KINDS:
        text = prompt_for(kind).lower()
        assert "start, stop or change a medication" in text


def test_every_prompt_permits_saying_it_cannot_read_the_image():
    """'I can't read that clearly' has to be an acceptable answer, or the model
    will guess instead."""
    for kind in KINDS:
        assert "unclear" in prompt_for(kind).lower()


def test_the_skin_prompt_refuses_to_name_a_condition():
    text = prompt_for("skin").lower()
    assert "do not name a condition" in text
    assert "cannot diagnose" in text


def test_the_medicine_prompt_refuses_to_identify_a_loose_tablet():
    """Colour and shape are not identification, and getting this wrong is
    dangerous."""
    text = prompt_for("medicine").lower()
    assert "loose tablet" in text
    assert "colour and shape are not identification" in text


def test_the_document_prompt_does_not_interpret_results():
    text = prompt_for("document").lower()
    assert "do not interpret" in text


# --------------------------------------------------------------------------- #
# The image reaches both providers correctly
# --------------------------------------------------------------------------- #
def test_the_image_block_is_base64_not_a_link():
    """A data URI, not the presigned URL — pointing a third party at that link
    would hand out access this service was careful to keep scoped."""
    block = to_image_block(DOC)
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    assert base64.standard_b64decode(block["source"]["data"]) == DOC.content
    assert "http" not in str(block)


def test_anthropic_puts_the_image_before_the_question():
    """The model reads better having seen the image before being asked."""
    msgs = _to_anthropic_messages(
        [UserMessage(content="what is this?", attachments=(to_image_block(DOC),))]
    )
    kinds = [b["type"] for b in msgs[0]["content"]]
    assert kinds == ["image", "text"]


def test_openai_translates_the_image_to_a_data_uri_part():
    msgs = _to_openai_messages(
        [UserMessage(content="what is this?", attachments=(to_image_block(DOC),))]
    )
    parts = msgs[0]["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[1]["type"] == "text"


def test_a_message_with_no_attachments_is_unchanged_on_both_providers():
    """The overwhelmingly common case must not gain a wrapper."""
    plain = [UserMessage(content="hello")]
    assert _to_anthropic_messages(plain) == [{"role": "user", "content": "hello"}]
    assert _to_openai_messages(plain) == [{"role": "user", "content": "hello"}]


# --------------------------------------------------------------------------- #
# Untrusted output
# --------------------------------------------------------------------------- #
async def test_a_vision_reply_is_still_subject_to_the_validator(vision_on):
    """A vision model is a generator like any other. What it says about a
    photograph is a claim, and the same guards apply."""
    from app.chat.validation import validate_reply
    from app.triage.red_flags import NONE

    provider = FakeProvider(
        turns=[LLMTurn(text="This rash means you probably have dengue.")]
    )
    result = await describe_image(provider, DOC, kind="skin")
    assert result is not None
    # The service does not sanitise — it reports. The ORCHESTRATOR's guards are
    # what refuse it, and they must.
    assert not validate_reply(result.text, NONE).ok


async def test_a_provider_failure_degrades_to_none(vision_on):
    provider = FakeProvider(raises=RuntimeError("vision model down"))
    assert await describe_image(provider, DOC) is None


async def test_an_empty_reply_is_not_a_result(vision_on):
    provider = FakeProvider(turns=[LLMTurn(text="   ")])
    assert await describe_image(provider, DOC) is None


async def test_an_empty_document_is_not_sent(vision_on):
    empty = FetchedDocument(b"", "image/jpeg", "reports", 1)
    provider = FakeProvider(turns=[LLMTurn(text="unused")])
    assert await describe_image(provider, empty) is None
    assert provider.calls == []


async def test_the_readers_question_reaches_the_model(vision_on):
    provider = FakeProvider(turns=[LLMTurn(text="ok")])
    await describe_image(provider, DOC, question="is this my sugar report?")
    sent = provider.calls[0]["messages"][0]
    assert sent.content == "is this my sugar report?"
