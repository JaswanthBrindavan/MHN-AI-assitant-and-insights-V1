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


def test_every_tracker_metric_enum_value_resolves():
    """The "reachable shape, unreachable guard" class, pinned.

    The tool offers ten metric words; each must resolve against the SAME
    _TRACKER_TERMS table the legacy parser reads. A value the resolver does not
    recognise is a tool the model can call and that always answers nothing --
    which is exactly how get_documents shipped broken on the agentic engine.
    """
    from app.chat.abilities import tracker_query_for
    from app.chat.tools.definitions import GET_TRACKER_TOTAL

    for value in GET_TRACKER_TOTAL.input_schema["properties"]["metric"]["enum"]:
        assert tracker_query_for(value, "week") is not None, value


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


# --------------------------------------------------------------------------- #
# get_trends_and_patterns — the assistant can see what app/patterns computes
# --------------------------------------------------------------------------- #
def test_the_trends_tool_is_offered():
    """Audit gap: ~2,200 lines of trend work, and chat imported one helper."""
    assert "get_trends_and_patterns" in {s.name for s in TOOL_SPECS}
    assert "get_trends_and_patterns" in EXECUTORS


def test_the_trends_tool_schema_is_a_deploy_constant():
    """The tool schemas are the larger half of the cached prefix. A schema
    that varied per reader or per flag would miss the cache on every turn."""
    from app.chat.tools.definitions import GET_TRENDS_AND_PATTERNS
    from app.patterns.service import TREND_METRICS

    first = json.dumps(GET_TRENDS_AND_PATTERNS.input_schema, sort_keys=True)
    second = json.dumps(GET_TRENDS_AND_PATTERNS.input_schema, sort_keys=True)
    assert first == second
    assert GET_TRENDS_AND_PATTERNS.input_schema["properties"]["metric"]["enum"] == list(
        TREND_METRICS
    )
    assert isinstance(TOOL_SPECS, tuple)


def _seed_wearable(db, user_id, *, days: int, short_last: bool = False):
    """A run of complete days ending yesterday: sleep, steps, and a habit."""
    from datetime import timedelta

    from app.models.common import tracking_today, utcnow
    from app.models.coredata import LifestyleLog, SahhaDailyTotal

    today = tracking_today()
    for i in range(1, days + 1):
        day = today - timedelta(days=i)
        # A short night on the most recent day, when asked for, so the
        # yesterday review has a move to talk about.
        sleep = 300.0 if (short_last and i == 1) else (360.0 if i <= days // 2 else 420.0)
        db.add(SahhaDailyTotal(
            user_id=user_id, metric="sleep_duration", bucket_start=day,
            total=sleep, entries=1, days_counted=1,
        ))
        db.add(SahhaDailyTotal(
            user_id=user_id, metric="steps", bucket_start=day,
            total=7000.0, entries=1, days_counted=1,
        ))
        db.add(LifestyleLog(
            user_id=user_id, log_type="coffee", quantity=1, unit="cup",
            logged_at=utcnow().replace(hour=20 if i <= days // 2 else 8)
            - timedelta(days=i),
        ))


async def _tool(db, user_id, **arguments):
    visuals: list[dict] = []
    result = await execute_tool(
        db, user_id, _call("get_trends_and_patterns", **arguments), None,
        visuals=visuals,
    )
    assert not result.is_error, result.content
    return json.loads(result.content), visuals


async def test_patterns_focus_returns_the_stored_artifacts(db_session):
    """Real rows the sweep wrote, served as the Insights screen serves them."""
    from app.patterns.engine import active_patterns, recompute_patterns

    user = uuid.uuid4()
    _seed_wearable(db_session, user, days=26)
    await db_session.flush()
    await recompute_patterns(db_session, user, reason="test")
    stored = {r.pattern_key for r in await active_patterns(db_session, user)}
    assert stored

    payload, _ = await _tool(db_session, user, focus="patterns")
    assert payload.get("found") is not False
    assert payload.get("computed") is not False
    assert {c["key"] for c in payload["patterns"]} <= stored
    coffee = next(c for c in payload["patterns"] if c["key"].startswith("coffee__sleep"))
    assert coffee["headline"] in payload["deterministic_reply"]
    # Observational wording only: no cause, no grade.
    assert "because" not in payload["deterministic_reply"].lower()
    # The raw floats behind the sentence stay out of the prompt — a mean in
    # minutes beside a sentence in hours is the fidelity trap.
    assert "mean_with" not in coffee and "difference" not in coffee


async def test_a_reader_with_no_data_is_told_not_enough_days_not_nothing_on_file(
    db_session,
):
    """No data is 'not enough days yet', with the count — never a bare
    'nothing on file', which the model reads as 'you have no records'."""
    payload, _ = await _tool(db_session, uuid.uuid4(), focus="patterns")
    assert payload["patterns"] == []
    assert len(payload["not_yet"]) > 0
    assert "enough days" in payload["deterministic_reply"]
    assert "Nothing on file" not in json.dumps(payload)

    payload, _ = await _tool(db_session, uuid.uuid4(), focus="trend",
                             metric="sleep_duration")
    assert payload["found"] is False
    assert "no sleep readings" in payload["deterministic_reply"]

    payload, _ = await _tool(db_session, uuid.uuid4(), focus="yesterday")
    assert payload["found"] is False
    assert "Nothing was recorded" in payload["deterministic_reply"]


async def test_a_reader_the_sweep_never_reached_is_computed_once_and_stored(
    db_session,
):
    """The sweep has never run in this deployment. A reader with 26 days of
    data and no artifact must get their patterns, not 'nothing on file' —
    and the rows are STORED, so the next read is a plain read."""
    from app.patterns.engine import active_patterns

    user = uuid.uuid4()
    _seed_wearable(db_session, user, days=26)
    await db_session.flush()
    assert not await active_patterns(db_session, user)

    payload, _ = await _tool(db_session, user, focus="patterns")
    assert payload.get("computed") is not False
    assert payload["patterns"] or payload["not_yet"]
    rows = await active_patterns(db_session, user)
    assert rows and all(r.recompute_reason == "first_use" for r in rows)


async def test_a_sweep_that_produced_nothing_is_not_reported_as_no_data(
    db_session, monkeypatch
):
    """Distinct from the empty-record case above: when neither the stored
    rows nor the one-off compute yield anything, the payload says 'not
    computed' and forbids the model from inferring an absence of data."""
    from app.chat.tools import executors

    async def _nothing(*_a, **_kw):
        return []

    monkeypatch.setattr(executors, "stored_cards", _nothing)
    payload, _ = await _tool(db_session, uuid.uuid4(), focus="patterns")
    assert payload["computed"] is False
    assert "NOT a statement that they have no data" in payload["note"]
    assert "found" not in payload


async def test_trend_focus_reports_this_week_against_last_with_a_chart(db_session):
    from app.chat.validation import validate_reply
    from app.grounding.fidelity import values_traceable
    from app.triage.red_flags import NONE

    user = uuid.uuid4()
    _seed_wearable(db_session, user, days=14)
    await db_session.flush()

    payload, visuals = await _tool(db_session, user, focus="trend",
                                   metric="sleep_duration")
    reply = payload["deterministic_reply"]
    assert "Over the last 7 days" in reply
    assert payload["direction"] == "down"
    assert "trending down" in reply
    # The series travels out of band, and the model is told a chart exists.
    assert visuals and visuals[0]["metric"] == "sleep_duration"
    assert visuals[0]["window_days"] == 14
    assert payload["chart_shown_to_reader"]["title"].startswith("Sleep")
    # A model quoting the sentence verbatim passes the fidelity guard, and
    # the sentence itself passes the validator.
    ok, stray = values_traceable(reply, [json.dumps(payload)])
    assert ok, stray
    assert validate_reply(reply, NONE).ok


async def test_yesterday_focus_returns_the_review_and_what_it_rests_on(db_session):
    from app.chat.validation import validate_reply
    from app.triage.red_flags import NONE

    user = uuid.uuid4()
    _seed_wearable(db_session, user, days=16, short_last=True)
    await db_session.flush()

    payload, _ = await _tool(db_session, user, focus="yesterday")
    assert payload.get("found") is not False
    assert payload["heading"]
    assert payload["reasoning"]
    assert "sleep_duration" in payload["rests_on"]
    assert "because" not in payload["deterministic_reply"].lower()
    assert validate_reply(payload["deterministic_reply"], NONE).ok


async def test_an_unknown_focus_or_metric_degrades_to_the_default_not_to_none(
    db_session,
):
    """None would become 'nothing on file' — an assertion about the reader's
    records made out of a model typo."""
    payload, _ = await _tool(db_session, uuid.uuid4(), focus="nonsense",
                             metric="deep_sleep")
    assert payload["metric"] == "sleep_duration"
    assert payload["provenance"]["path"] == "patterns_trend"
