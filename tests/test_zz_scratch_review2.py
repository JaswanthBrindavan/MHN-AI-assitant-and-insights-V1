from __future__ import annotations

import uuid

from app.chat.data_handlers import handle_document_query
from app.chat.tools.registry import execute_tool
from app.llm.tools import ToolCall
from app.models.common import utcnow


async def test_payload(db_session):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(
        Report(
            user_id=user_id,
            filepath="reports/cbc.pdf",
            private=False,
            created_at=utcnow(),
            content={
                "ai": {
                    "classification": {"section": "reports", "title": "CBC"},
                    "extraction": {"results": []},
                }
            },
        )
    )
    await db_session.flush()
    for phrase in ("show me reports", "show my reports", "show me my reports"):
        r = await handle_document_query(db_session, user_id, phrase)
        print(phrase, "->", None if r is None else r.get("documents"))
    res = await execute_tool(
        db_session,
        user_id,
        ToolCall(id="c1", name="get_documents", arguments={"kinds": ["reports"]}),
    )
    print("PAYLOAD:", res.content)
