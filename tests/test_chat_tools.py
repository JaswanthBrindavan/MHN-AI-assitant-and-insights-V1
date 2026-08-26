"""Tool executors — structured data out, never an exception.

The registry's contract is the load-bearing part: a tool that raises would kill
a patient-facing turn, and a tool that leaks its transaction failure would
poison every lookup after it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.chat.tools.definitions import TOOL_SPECS
from app.chat.tools.registry import EXECUTORS, execute_tool
from app.llm.tools import ToolCall


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=arguments)


# --------------------------------------------------------------------------- #
# Schema hygiene
# --------------------------------------------------------------------------- #
def test_every_spec_has_a_strict_object_schema():
    """A loose schema is how an open-weight model produces arguments the
    executor cannot use."""
    assert TOOL_SPECS
    for spec in TOOL_SPECS:
        schema = spec.input_schema
        assert schema["type"] == "object", spec.name
        assert schema.get("additionalProperties") is False, spec.name
        assert "properties" in schema, spec.name
        assert isinstance(schema.get("required"), list), spec.name


def test_every_required_field_is_declared_as_a_property():
    for spec in TOOL_SPECS:
        for field in spec.input_schema["required"]:
            assert field in spec.input_schema["properties"], f"{spec.name}.{field}"


def test_every_spec_has_a_description_written_for_the_model():
    for spec in TOOL_SPECS:
        assert len(spec.description.strip()) > 40, spec.name


def test_spec_names_and_executors_match_exactly():
    assert {s.name for s in TOOL_SPECS} == set(EXECUTORS)


def test_tool_names_are_unique():
    names = [s.name for s in TOOL_SPECS]
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Dispatch contract
# --------------------------------------------------------------------------- #
async def test_a_hallucinated_tool_name_returns_an_error_not_an_exception(
    db_session,
):
    result = await execute_tool(
        db_session, uuid.uuid4(), _call("no_such_tool"), None
    )
    assert result.is_error
    assert result.call_id == "c1"
    # The model is told what it CAN call, so it stops guessing.
    assert "get_latest_metric" in json.loads(result.content)["error"]


async def test_non_dict_arguments_are_rejected(db_session):
    bad = ToolCall(id="c1", name="get_latest_metric", arguments="not a dict")  # type: ignore[arg-type]
    result = await execute_tool(db_session, uuid.uuid4(), bad, None)
    assert result.is_error


async def test_an_executor_crash_is_isolated_and_the_session_survives(
    db_session, monkeypatch
):
    """The whole point of the SAVEPOINT: one bad tool must not poison the rest
    of the turn."""
    from app.chat.tools import executors

    async def _boom(*_a, **_kw):
        raise RuntimeError("core table missing")

    monkeypatch.setattr(executors, "get_latest_metric", _boom)
    monkeypatch.setitem(EXECUTORS, "get_latest_metric", _boom)

    result = await execute_tool(
        db_session, uuid.uuid4(), _call("get_latest_metric", metric="hba1c"), None
    )
    assert result.is_error
    assert "could not be completed" in json.loads(result.content)["error"]

    # The session is still usable.
    assert (await db_session.execute(text("SELECT 1"))).scalar() == 1


async def test_a_crash_does_not_leak_arguments_into_the_log(
    db_session, monkeypatch, caplog
):
    """Tool arguments can carry PHI — they must never reach the log."""
    from app.chat.tools import executors

    async def _boom(*_a, **_kw):
        raise RuntimeError("nope")

    monkeypatch.setitem(EXECUTORS, "get_report_parameter", _boom)
    monkeypatch.setattr(executors, "get_report_parameter", _boom)

    with caplog.at_level("WARNING"):
        await execute_tool(
            db_session,
            uuid.uuid4(),
            _call("get_report_parameter", parameter="SECRETVALUE123"),
            None,
        )
    assert "SECRETVALUE123" not in caplog.text


async def test_nothing_on_file_is_a_result_not_an_error(db_session):
    """'No data' is a real answer. Flagging it as an error would push the model
    toward retrying or estimating.

    The handler answers this case itself with its own validator-safe wording,
    so the model gets a sentence it can quote rather than a bare flag.
    """
    result = await execute_tool(
        db_session, uuid.uuid4(), _call("get_latest_metric", metric="hba1c"), None
    )
    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["found"] is False
    assert "couldn't find" in payload["deterministic_reply"]


async def test_an_unanswerable_argument_falls_back_to_the_registry_not_found(
    db_session,
):
    """When the executor itself returns None (no usable argument), the registry
    supplies the not-found payload — including the instruction not to guess."""
    result = await execute_tool(
        db_session, uuid.uuid4(), _call("get_latest_metric", metric=""), None
    )
    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["found"] is False
    assert "do not estimate" in payload["note"]


# --------------------------------------------------------------------------- #
# Real data through a real handler
# --------------------------------------------------------------------------- #
@pytest.fixture
async def user_with_hba1c(db_session):
    from app.models.coredata import Report

    user_id = uuid.uuid4()
    db_session.add(
        Report(
            id=901,
            user_id=user_id,
            filepath="reports/abc",
            private=False,
            content={
                "ai": {
                    "classification": {"section": "reports", "title": "Lab report"},
                    "extraction": {
                        "results": [
                            {
                                "test_name": "HbA1c",
                                "value": "6.1",
                                "unit": "%",
                                "value_numeric": 6.1,
                                "abnormal_flag": "high",
                            }
                        ]
                    },
                }
            },
        )
    )
    await db_session.flush()
    return user_id


async def test_report_parameter_returns_data_and_the_vetted_wording(
    db_session, user_with_hba1c
):
    result = await execute_tool(
        db_session, user_with_hba1c, _call("get_report_parameter", parameter="HbA1c"),
        None,
    )
    payload = json.loads(result.content)
    assert not result.is_error
    # The clinically-reviewed phrasing travels with the data, so the model can
    # quote it verbatim rather than paraphrasing a lab value.
    assert "6.1" in payload["deterministic_reply"]
    assert payload["parameter"] == "HbA1c"


async def test_the_tool_payload_is_a_valid_fidelity_source(
    db_session, user_with_hba1c
):
    """End-to-end tie-in: a value quoted from a tool result must pass the
    numeric-fidelity guard, and a drifted one must not."""
    from app.grounding.fidelity import values_traceable

    result = await execute_tool(
        db_session, user_with_hba1c, _call("get_report_parameter", parameter="HbA1c"),
        None,
    )
    sources = [result.content]

    ok, _ = values_traceable("Your HbA1c was 6.1%.", sources)
    assert ok

    ok, stray = values_traceable("Your HbA1c was 6.5%.", sources)
    assert not ok and stray == ["6.5%"]


# --------------------------------------------------------------------------- #
# Regression: multiple tool calls in one turn must ALL succeed
# --------------------------------------------------------------------------- #
async def test_several_tool_calls_in_one_turn_all_succeed(
    db_session, user_with_hba1c
):
    """Found by review: executing tool calls with asyncio.gather made only the
    FIRST succeed. Every executor shares one AsyncSession, and SQLAlchemy
    refuses concurrent operations on one ("This session is provisioning a new
    connection"). The rest came back "could not be completed" on perfectly good
    data — and the model would then tell the reader their records are
    unavailable when they are not.

    run_agent executes sequentially for exactly this reason. This test fails if
    anyone reintroduces gather.
    """
    from app.chat.agent import run_agent
    from app.llm.fake import FakeProvider
    from app.llm.tools import LLMTurn, UserMessage

    calls = (
        ToolCall(id="a", name="get_report_parameter", arguments={"parameter": "HbA1c"}),
        ToolCall(id="b", name="get_family_members", arguments={}),
        ToolCall(id="c", name="get_health_summary", arguments={"period": "week"}),
    )
    provider = FakeProvider(
        turns=[
            LLMTurn(tool_calls=calls, stop_reason="tool_use"),
            LLMTurn(text="Here is a combined answer."),
        ]
    )

    async def _executor(call):
        return await execute_tool(db_session, user_with_hba1c, call, None)

    out = await run_agent(
        provider, "sys", [UserMessage("how am I doing?")], TOOL_SPECS, _executor
    )

    failures = [s for s in out.source_texts if "could not be completed" in s]
    assert not failures, f"{len(failures)} of {len(calls)} tool calls failed"

    # And the session is still usable for everything that follows.
    assert (await db_session.execute(text("SELECT 1"))).scalar() == 1
