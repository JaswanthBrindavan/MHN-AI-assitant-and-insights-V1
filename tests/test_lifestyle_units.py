"""Davi writes lifestyle rows in the unit mhn-spring says the column is in.

`lifestyle_log` is a SHARED table. mhn-spring's `LifestyleMetric` fixes one
unit per type (water and alcohol in millilitres, coffee/tea in cups, smoking
counted), `resolveUnit` rejects anything else with a 400, and both the write
fan-out (`MetricFanout.of`) and the nightly reconciler
(`ManualTrackingReconciler.MEASURES`) sum `quantity` for the primary metric.

So a row written as `quantity=2, unit='glass'` does not merely read oddly: it
lands in the reader's own water series in the app as 2 ml. These pin the
conversion, the refusal when no sanctioned size exists, and the plain
`SUM(quantity)` read that is unit-safe once the write is.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.chat.data_handlers import (
    handle_summary_query,
    handle_tracker_add,
    handle_tracker_query,
)
from app.coredata.service import (
    LIFESTYLE_UNITS,
    add_lifestyle_log,
    canonical_amount,
    lifestyle_phrase,
    lifestyle_totals,
    window_start,
)
from app.models.common import utcnow
from app.models.coredata import LifestyleLog

USER = uuid.UUID("44444444-4444-4444-4444-444444444444")


def _log(db, log_type, quantity, unit, *, days=1):
    """A row exactly as mhn-spring would have left it: canonical unit."""
    db.add(LifestyleLog(
        user_id=USER, log_type=log_type, quantity=quantity, unit=unit,
        logged_at=utcnow() - timedelta(days=days),
    ))


async def _totals(db):
    await db.flush()
    return await lifestyle_totals(db, USER, window_start("week"))


# --------------------------------------------------------------------------- #
# canonical_amount — the one place a spoken unit becomes a stored one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("log_type", "quantity", "unit", "kind", "expected"),
    [
        # Sanctioned sizes, every one a row of V35's `drink_serving_size`.
        ("water", 2, "glass", None, (500.0, "ml")),
        ("water", 1, "bottle", None, (500.0, "ml")),
        ("water", 1, "litre", None, (1000.0, "ml")),
        ("water", 500, "ml", None, (500.0, "ml")),
        # The vessel is sized by the DRINK, not by the roll-up bucket: V35 is
        # keyed per category "because 'Large' is 350 ml of coffee, 500 ml of
        # beer and 90 ml of whisky".
        ("alcohol", 1, "bottle", "beer", (330.0, "ml")),    # beer/Bottle
        ("alcohol", 1, "can", "beer", (500.0, "ml")),       # beer/Can
        ("alcohol", 1, "pint", "beer", (568.0, "ml")),      # beer/Pint
        ("alcohol", 2, "glass", "wine", (300.0, "ml")),     # wine/Glass x2
        ("alcohol", 1, "peg", "whisky", (60.0, "ml")),      # spirits/Peg
        ("alcohol", 1, "shot", "vodka", (30.0, "ml")),      # spirits/Small peg
        # Servings are unit-free: the vessel changes the noun, not the number.
        ("coffee", 3, "cup", "coffee", (3.0, "cup")),
        ("tea", 1, "mug", "chai", (1.0, "cup")),
        ("smoking", 5, "cigarette", None, (5.0, "count")),
        ("smoking", 2, "beedi", None, (2.0, "count")),
        ("energy_drink", 1, "can", None, (1.0, "serving")),
        # No unit at all means the canonical one.
        ("water", 250, None, None, (250.0, "ml")),
    ],
)
def test_canonical_amount_converts_to_the_stored_unit(
    log_type, quantity, unit, kind, expected
):
    assert canonical_amount(log_type, quantity, unit, kind) == expected


@pytest.mark.parametrize(
    ("log_type", "unit", "kind"),
    [
        ("alcohol", "drink", None),   # "2 drinks" — no size for it anywhere
        ("water", "cup", None),       # no water/Cup row in `drink_serving_size`
        # THE ONE THIS TABLE EXISTS FOR. The seed has no wine/Bottle row at
        # all, so 330 (beer's bottle) is not "the sanctioned size for a wine
        # bottle" — it is a 56% under-report written into a shared table.
        ("alcohol", "bottle", "wine"),
        # ...and 150 is wine's glass, not beer's (smallest seeded beer serving
        # is 330) and not a spirit's (30/60/90).
        ("alcohol", "glass", "beer"),
        ("alcohol", "glass", "whisky"),
        # A drink with no V35 category has no sanctioned vessel at all.
        ("alcohol", "bottle", "alcohol"),
    ],
)
def test_no_sanctioned_size_is_refused_not_guessed(log_type, unit, kind):
    assert canonical_amount(log_type, 2, unit, kind) is None


def test_every_log_type_has_a_stored_unit_and_a_reader_noun():
    for log_type, (stored, noun) in LIFESTYLE_UNITS.items():
        assert stored in ("ml", "cup", "count", "serving"), log_type
        assert noun


# --------------------------------------------------------------------------- #
# The write side
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_add_lifestyle_log_stores_the_canonical_unit(db_session):
    glasses = await add_lifestyle_log(db_session, USER, "water", 2, "glass")
    cups = await add_lifestyle_log(db_session, USER, "coffee", 3, "cup")
    cigs = await add_lifestyle_log(db_session, USER, "smoking", 5, "cigarette")

    # 2 glasses is 500 ml, not 2 ml in the reader's own hydration chart.
    assert (float(glasses.quantity), glasses.unit) == (500.0, "ml")
    assert glasses.volume_ml == 500.0 and glasses.servings is None
    assert (float(cups.quantity), cups.unit) == (3.0, "cup")
    assert cups.servings == 3.0 and cups.volume_ml is None
    assert (float(cigs.quantity), cigs.unit) == (5.0, "count")


@pytest.mark.asyncio
async def test_add_lifestyle_log_refuses_a_unit_spring_would_reject(db_session):
    # mhn-spring answers this with a 400; a row written anyway corrupts a
    # table Davi does not own.
    with pytest.raises(ValueError, match="no sanctioned size"):
        await add_lifestyle_log(db_session, USER, "alcohol", 2, "drink")


@pytest.mark.asyncio
async def test_chat_add_converts_and_says_so(db_session):
    out = await handle_tracker_add(db_session, USER, "drank two glasses of water")

    assert out is not None
    assert "2 glasses of water (500 ml)" in out["reply"]
    row = (await _totals(db_session))["water"]
    assert row.total == 500.0 and row.unit == "ml"


@pytest.mark.asyncio
async def test_chat_add_asks_rather_than_inventing_a_size(db_session):
    out = await handle_tracker_add(db_session, USER, "i had 2 drinks last night")

    assert out is not None
    assert out["provenance"]["declined"] == "no_sanctioned_size"
    assert "millilitres" in out["reply"]
    assert not (await _totals(db_session))


@pytest.mark.asyncio
async def test_a_beer_has_a_sanctioned_size_and_is_logged(db_session):
    out = await handle_tracker_add(db_session, USER, "had a beer last night")

    assert out is not None
    assert out["provenance"].get("declined") is None
    assert (await _totals(db_session))["alcohol"].total == 330.0


@pytest.mark.asyncio
async def test_a_bottle_of_wine_is_not_written_as_a_beer_bottle(db_session):
    """The whole point of keying on the drink. V35 has no wine/Bottle row, so
    330 was BEER's bottle applied to wine — a 56% under-report of alcohol in a
    table the app's charts and `lifestyle_limit` read, left behind after the
    conversation ends."""
    out = await handle_tracker_add(db_session, USER, "i had 2 bottles of wine")

    assert out is not None
    assert out["provenance"]["declined"] == "no_sanctioned_size"
    # And the reply names the DRINK, so the reader can see what was not sized.
    assert "wine" in out["reply"]
    assert not (await _totals(db_session))


@pytest.mark.asyncio
async def test_a_glass_of_whisky_is_not_written_as_a_wine_glass(db_session):
    out = await handle_tracker_add(db_session, USER, "had a glass of whisky")

    assert out is not None
    assert out["provenance"]["declined"] == "no_sanctioned_size"
    assert not (await _totals(db_session))


@pytest.mark.asyncio
async def test_the_echo_names_the_drink_not_the_log_type(db_session):
    """"2 bottles of alcohol (660 ml)" hid the substitution the echo exists to
    let the reader correct."""
    out = await handle_tracker_add(db_session, USER, "i had 2 bottles of beer")

    assert out is not None
    assert "2 bottles of beer (660 ml)" in out["reply"]
    assert (await _totals(db_session))["alcohol"].total == 660.0


# --------------------------------------------------------------------------- #
# The read side: a plain SUM(quantity), which the canonical write makes safe
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("log_type", "quantity", "unit", "expected"),
    [
        ("water", 1250, "ml", "1250 ml"),
        ("alcohol", 330, "ml", "330 ml"),
        ("coffee", 3, "cup", "3 cups"),
        ("tea", 1, "cup", "1 cup"),
        ("smoking", 5, "count", "5 cigarettes"),
        ("energy_drink", 2, "serving", "2 servings"),
        ("other_drink", 1, "serving", "1 serving"),
    ],
)
@pytest.mark.asyncio
async def test_every_log_type_totals_with_its_reader_noun(
    db_session, log_type, quantity, unit, expected
):
    _log(db_session, log_type, quantity, unit)

    total = (await _totals(db_session))[log_type]

    assert total.text() == expected
    assert total.total == float(quantity)


@pytest.mark.asyncio
async def test_water_logged_twice_adds_up_in_millilitres(db_session):
    # What used to be "2 glasses and 500 ml" -> 502. Both rows are ml now.
    await add_lifestyle_log(db_session, USER, "water", 2, "glass")
    await add_lifestyle_log(db_session, USER, "water", 500, "ml")

    water = (await _totals(db_session))["water"]

    assert water.total == 1000.0 and water.unit == "ml"
    assert "502" not in water.text()


@pytest.mark.asyncio
async def test_tracker_reply_carries_the_unit(db_session):
    await add_lifestyle_log(db_session, USER, "water", 2, "glass")

    # A ROLLING ask on purpose: it reads `lifestyle_log`, where the row just
    # written is already visible. "this week" is a calendar window and reads
    # Spring's overnight daily totals instead.
    out = await handle_tracker_query(
        db_session, USER, "how much water did I drink"
    )

    assert out is not None
    assert "500 ml of water" in out["reply"]


@pytest.mark.asyncio
async def test_smoking_phrase_does_not_name_the_kind_twice(db_session):
    _log(db_session, "smoking", 2, "count")

    assert lifestyle_phrase((await _totals(db_session))["smoking"]) == "2 cigarettes"


@pytest.mark.asyncio
async def test_summary_chart_never_mixes_units_in_one_series(db_session):
    _log(db_session, "coffee", 3, "cup")
    _log(db_session, "tea", 2, "cup")
    _log(db_session, "water", 500, "ml")
    await db_session.flush()

    out = await handle_summary_query(db_session, USER, "health summary for the week")

    assert out is not None
    visual = out["visual"]
    assert visual["unit"] == "cup"
    assert visual["labels"] == ["coffee", "tea"] and visual["values"] == [3.0, 2.0]
    # The millilitre series is still REPORTED, just not plotted beside cups.
    assert "500 ml of water" in out["reply"]
