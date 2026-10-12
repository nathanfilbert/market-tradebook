"""Lifecycle projection: exact replay from a proven flat point, fail-closed otherwise."""
import json
import random
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tradebook.coinbase_products import classify_product
from tradebook.coinbase_source import CoinbaseCFMDatedFutureSource
from tradebook.lifecycles import FlatBaseline, lifecycle_id, project, replay_scope
from tradebook.store import connect, initialize, update_reason, upsert_packets_and_events

SOURCE, ACCOUNT, PRODUCT = "coinbase.cfm.dated_future", "portfolio", "BIP-20DEC30-CDE"


def at(minute: int) -> str:
    return f"2026-10-01T12:{minute:02}:00Z"


def fill(entry_id, side, qty, minute, *, price="100", product=PRODUCT, account=ACCOUNT, commission="0.30", **extra):
    return {"entry_id": entry_id, "trade_id": entry_id, "order_id": "o-" + entry_id, "trade_time": at(minute),
            "sequence_timestamp": at(minute), "trade_type": "FILL", "price": price, "size": str(qty),
            "commission": commission, "product_id": product, "size_in_quote": False, "side": side,
            "retail_portfolio_id": account, **extra}


def baseline(minute=0, product=PRODUCT, account=ACCOUNT):
    return FlatBaseline(SOURCE, account, product, datetime.fromisoformat(at(minute)), "fixture evidence")


def replay(fills, baselines=(baseline(),), product=PRODUCT):
    return replay_scope(fills, baselines, source_key=SOURCE, account_id=ACCOUNT, product_id=product)


def summary(scope):
    return [(lc.status, lc.side, str(lc.open_quantity), [(f["entry_id"], f["role"], f["quantity"]) for f in lc.fills])
            for lc in scope.lifecycles]


def test_without_flat_evidence_a_replay_reaching_zero_is_not_trusted():
    # Same shape as the live LNP history: short, cover, short again. Zero is calculated, not proven.
    scope = replay([fill("a", "SELL", 1, 1), fill("b", "BUY", 1, 2), fill("c", "SELL", 1, 3)], baselines=())
    assert scope.status == "unavailable" and scope.lifecycles == ()
    assert "no proven flat" in scope.reason


def test_open_close_then_flat_reopen_gets_a_new_identity():
    scope = replay([fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2), fill("c", "SELL", 2, 3)])
    assert scope.status == "verified"
    first, second = scope.lifecycles
    assert first.lifecycle_id == lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "a")
    assert (first.status, first.side, first.closed_at.minute) == ("closed", "long", 2)
    assert (second.status, second.side, second.open_quantity) == ("open", "short", Decimal(2))
    assert second.lifecycle_id == lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "c")


def test_adds_and_partial_closes_share_one_lifecycle():
    scope = replay([fill("a", "BUY", 1, 1, price="100"), fill("b", "BUY", 3, 2, price="120"),
                    fill("c", "SELL", 1, 3), fill("d", "SELL", 2, 4)])
    (lc,) = scope.lifecycles
    view = lc.public()
    assert [f["role"] for f in lc.fills] == ["open", "add", "close", "close"]
    assert (lc.status, view["open_quantity"], view["opened_quantity"]) == ("open", "1", "4")
    assert view["weighted_entry_price"] == "115" and view["adds"] == 1 and view["closes"] == 2


def test_reversal_is_split_exactly_with_fees_summing_to_the_fill():
    scope = replay([fill("a", "BUY", 1, 1), fill("rev", "SELL", 3, 2, commission="0.10")])
    old, new = scope.lifecycles
    assert summary(scope) == [("closed", "long", "0", [("a", "open", "1"), ("rev", "close", "1")]),
                              ("open", "short", "2", [("rev", "residual_open", "2")])]
    assert new.lifecycle_id == lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "rev", residual=True)
    assert new.lifecycle_id != lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "rev")
    fees = Decimal(old.fills[1]["fee_share"]) + Decimal(new.fills[0]["fee_share"])
    assert fees == Decimal("0.10")


def test_same_time_fills_in_both_directions_are_unresolved_not_ordered_by_id():
    scope = replay([fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2), fill("c", "BUY", 1, 2)])
    assert scope.status == "unresolved" and "order unproven" in scope.reason
    assert scope.lifecycles[0].status == "unresolved"


def test_same_time_partial_fills_on_one_side_replay_normally():
    scope = replay([fill("a2", "SELL", 1, 1), fill("a1", "SELL", 1, 1), fill("b", "BUY", 2, 3)])
    (lc,) = scope.lifecycles
    assert lc.opening_entry_id == "a1" and lc.status == "closed"


def test_history_starting_mid_position_is_ignored_until_the_flat_point():
    fills = [fill("old", "SELL", 1, 1), fill("cover", "BUY", 1, 5), fill("new", "BUY", 1, 10)]
    scope = replay(fills, baselines=(baseline(minute=7),))
    assert summary(scope) == [("open", "long", "1", [("new", "open", "1")])]
    # A flat claim that contradicts the replayed position fails closed instead of guessing.
    contradicted = replay(fills, baselines=(baseline(0), baseline(3)))
    assert contradicted.lifecycles[0].status == "unresolved"
    assert "fills replay to -1" in contradicted.lifecycles[0].unresolved_reason


@pytest.mark.parametrize("bad", [
    fill("x", "BUY", 1, 3, trade_type="CORRECTION"), fill("x", "BUY", 1, 3, trade_type="REVERSAL"),
    fill("x", "BUY", 1, 3, account="other"), fill("x", "BUY", 1, 3, size_in_quote=True),
    fill("x", "BUY", 1, 3, position_effect="OPEN"), fill("x", "BUY", "0", 3)])
def test_non_replayable_events_mark_the_scope_unresolved_until_the_next_flat_point(bad):
    scope = replay([fill("a", "SELL", 1, 1), bad, fill("b", "BUY", 1, 5), fill("c", "BUY", 1, 9)],
                   baselines=(baseline(0), baseline(7)))
    first, last = scope.lifecycles
    assert first.status == "unresolved" and first.unresolved_reason
    assert (last.opening_entry_id, last.status, scope.status) == ("c", "open", "verified")


def test_close_with_nothing_open_is_unresolved():
    scope = replay([fill("a", "SELL", 1, 1, position_effect="CLOSE")])
    assert scope.status == "unresolved" and scope.lifecycles == ()


def test_replay_is_idempotent_and_independent_of_input_order():
    fills = [fill(f"f{i}", side, qty, i) for i, (side, qty) in
             enumerate([("BUY", 1), ("BUY", 2), ("SELL", 4), ("BUY", 1), ("SELL", 2), ("BUY", 3)], start=1)]
    expected = [lc.public() for lc in replay(fills).lifecycles]
    for seed in range(5):
        shuffled = list(fills)
        random.Random(seed).shuffle(shuffled)
        assert [lc.public() for lc in replay(shuffled).lifecycles] == expected
    assert json.dumps(expected, sort_keys=True) == json.dumps([lc.public() for lc in replay(fills).lifecycles], sort_keys=True)


def test_late_arriving_earlier_fill_recomputes_deterministically():
    before = replay([fill("b", "BUY", 1, 5), fill("c", "SELL", 1, 6)])
    after = replay([fill("b", "BUY", 1, 5), fill("c", "SELL", 1, 6), fill("a", "BUY", 1, 2)])
    assert [lc.opening_entry_id for lc in before.lifecycles] == ["b"]
    assert summary(after) == [("open", "long", "1", [("a", "open", "1"), ("b", "add", "1"), ("c", "close", "1")])]
    assert {lc.lifecycle_id for lc in before.lifecycles}.isdisjoint({lc.lifecycle_id for lc in after.lifecycles})


# --- Saved database: scopes, separation and exact links to closed trades -------------------------

def spec(product=PRODUCT):
    return classify_product({"product_id": product, "product_type": "FUTURE", "quote_currency_id": "USD",
                             "future_product_details": {"contract_expiry_type": "EXPIRING",
                                                        "risk_managed_by": "MANAGED_BY_FCM"}},
                            account_family="CFM", source_reference="fixture")


def save(db, fills, product=PRODUCT, account=ACCOUNT):
    source = CoinbaseCFMDatedFutureSource(account, spec(product), fills)
    packets = list(source.iter_closes())
    upsert_packets_and_events(db, packets, source_key=SOURCE, account_id=account, fills=fills)
    return packets


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "book.sqlite3")
    initialize(connection)
    yield connection
    connection.close()


def test_exact_links_keep_existing_trade_ids_reasons_and_notes(db):
    save(db, [fill("a", "BUY", 1, 1), fill("b", "BUY", 1, 2), fill("c", "SELL", 1, 3),
              fill("d", "SELL", 1, 4), fill("e", "SELL", 1, 5)])
    trades = {row[0]: row[1] for row in db.execute("SELECT id, close_id FROM trades")}
    assert trades
    trade_id = next(iter(trades))
    update_reason(db, trade_id, "breakout", 0)
    before = [tuple(r) for r in db.execute("SELECT * FROM trades ORDER BY id")]
    without = project(db)
    assert all("unresolved" in link and "no proven flat" in link["unresolved"] for link in without.links.values())
    with_flat = project(db, (baseline(),))
    first = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "a")
    assert {link["lifecycle_id"] for link in with_flat.links.values()} == {first}
    assert with_flat.lifecycles[first].status == "closed"
    assert with_flat.lifecycles[lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "e")].status == "open"
    assert [tuple(r) for r in db.execute("SELECT * FROM trades ORDER BY id")] == before


def test_products_and_accounts_never_mix(db):
    save(db, [fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2)])
    save(db, [fill("x", "SELL", 1, 1, product="BTC-20DEC30-CDE")], product="BTC-20DEC30-CDE")
    save(db, [fill("y", "SELL", 1, 1, account="other")], account="other")
    flats = (baseline(), baseline(product="BTC-20DEC30-CDE"))
    result = project(db, flats)
    scopes = {(s.account_id, s.product_id): s for s in result.scopes}
    assert set(scopes) == {(ACCOUNT, PRODUCT), (ACCOUNT, "BTC-20DEC30-CDE"), ("other", PRODUCT)}
    assert scopes[("other", PRODUCT)].status == "unavailable"  # a baseline for one account proves nothing for another
    assert [lc.side for lc in scopes[(ACCOUNT, "BTC-20DEC30-CDE")].lifecycles] == ["short"]
    assert [lc.status for lc in scopes[(ACCOUNT, PRODUCT)].lifecycles] == ["closed"]


def test_projection_is_identical_across_runs_and_writes_nothing(db):
    save(db, [fill("a", "BUY", 2, 1), fill("b", "SELL", 1, 2), fill("c", "SELL", 3, 3)])
    changes = db.total_changes
    first, second = project(db, (baseline(),)), project(db, (baseline(),))
    assert [lc.public() for lc in first.lifecycles.values()] == [lc.public() for lc in second.lifecycles.values()]
    assert first.links == second.links
    assert db.total_changes == changes


def test_changed_copy_of_a_saved_fill_is_refused_and_projection_is_unchanged(db):
    fills = [fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2)]
    save(db, fills)
    expected = [lc.public() for lc in project(db, (baseline(),)).lifecycles.values()]
    with pytest.raises(ValueError, match="source drift"):
        upsert_packets_and_events(db, [], source_key=SOURCE, account_id=ACCOUNT,
                                  fills=[fill("b", "SELL", 2, 2)])
    assert [lc.public() for lc in project(db, (baseline(),)).lifecycles.values()] == expected


def test_reversal_close_is_unlinked_until_a_matching_close_exists(db):
    save(db, [fill("a", "BUY", 1, 1), fill("rev", "SELL", 3, 2)])
    result = project(db, (baseline(),))
    statuses = sorted(lc.status for lc in result.lifecycles.values())
    assert statuses == ["closed", "open"]
    # Today's close builder records the reversal close; the link must point at the old lifecycle only.
    for link in result.links.values():
        assert link.get("lifecycle_id") in (None, lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "a"))


def test_spot_rows_are_not_projected(db):
    db.execute("INSERT INTO source_events VALUES ('coinbase.spot', ?, ?, ?)",
               (ACCOUNT, json.dumps(["fills", "s"], separators=(",", ":")), json.dumps(fill("s", "BUY", 1, 1))))
    assert project(db, (baseline(),)).scopes == ()


def test_deployed_notes_columns_are_compatible(db):
    # The running checkout adds notes, notes_revision and notes_history; lifecycles must not care.
    db.execute("ALTER TABLE trades ADD COLUMN notes TEXT")
    db.execute("ALTER TABLE trades ADD COLUMN notes_revision INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE TABLE notes_history (trade_id TEXT, revision INTEGER, notes TEXT, edited_at TEXT)")
    save(db, [fill("a", "BUY", 1, 1), fill("b", "SELL", 1, 2)])
    db.execute("UPDATE trades SET notes='kept', notes_revision=1")
    result = project(db, (baseline(),))
    assert all("lifecycle_id" in link for link in result.links.values())
    assert {r[0] for r in db.execute("SELECT notes FROM trades")} == {"kept"}


def test_baseline_times_with_offsets_compare_as_instants():
    # 12:07+01:00 is 11:07Z, before both fills, so both replay.
    as_of = datetime.fromisoformat("2026-10-01T12:07:00+01:00")
    scope = replay([fill("a", "BUY", 1, 5), fill("b", "SELL", 1, 9)],
                   baselines=(FlatBaseline(SOURCE, ACCOUNT, PRODUCT, as_of, "fixture"),))
    assert [lc.status for lc in scope.lifecycles] == ["closed"]
