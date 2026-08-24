from __future__ import annotations

import uuid

import pytest

from app.chat.orchestrator import handle_chat
from app.llm.fake import FakeProvider
from app.llm.tools import LLMTurn, ToolCall


@pytest.fixture(autouse=True)
def _agentic(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CHAT_ENGINE", "agentic")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _mk_user(db_session):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(
        Report(
            id=9021,
            user_id=user_id,
            filepath="reports/cbc.pdf",
            private=False,
            content={
                "ai": {
                    "classification": {"section": "reports", "title": "CBC"},
                    "extraction": {"results": []},
                }
            },
        )
    )
    await db_session.flush()
    return user_id


async def test_documents_lost_on_agentic(db_session):
    user_id = await _mk_user(db_session)
    provider = FakeProvider(
        turns=[
            LLMTurn(
                tool_calls=(
                    ToolCall(id="c1", name="get_documents", arguments={"kinds": ["report"]}),
                ),
                stop_reason="tool_use",
            ),
            LLMTurn(text="You have one report on file: CBC."),
        ]
    )
    result = await handle_chat(db_session, user_id, "show me my reports", provider)
    print("REPLY:", result.response_message)
    print("DOCUMENTS:", result.documents)
    print("VISUAL:", result.visual)
    print("CITATIONS:", result.citations)
    print("PROV:", result.provenance)
    # what the model actually saw
    for c in provider.calls:
        pass
