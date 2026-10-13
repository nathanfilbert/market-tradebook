"""Regressions for the 02b3d0e review: exit allocation proof, and legacy edits racing the first shared save."""
import json
import sqlite3
from datetime import timedelta

import pytest

from tradebook import position_reasons as pr
from tradebook.domain import ClosedTradePacket
from tradebook.lifecycles import lifecycle_id, project
from tradebook.position_reasons import entry_group_id, views_by_trade
from tradebook.store import canonical_json, connect, initialize, update_reason, upsert_packet
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save
from test_position_reasons import AVAX, AVAX_FILLS, GROUP
from test_positions import take


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "book.sqlite3"
    db = connect(p); initialize(db); db.close()
    return p


def book(path, fills, product=AVAX):
    with connect(path) as db:
        save(db, fills, product=product)


def views(path):
    with connect(path) as db:
        return views_by_trade(db, project(db))


def trade_ids(path):
    with connect(path) as db:
        return [r[0] for r in db.execute("SELECT id FROM trades ORDER BY close_time")]


def rewrite(path, trade_id, *, packet=None, fills=None, row=None, raw_packet=None):
    """Change one close's packet (row follows it), its stored packet JSON alone, saved raw fills, or the row."""
    with connect(path) as db:
        if raw_packet:
            current = json.loads(db.execute("SELECT packet_json FROM trades WHERE id=?", (trade_id,)).fetchone()[0])
            raw_packet(current)
            db.execute("UPDATE trades SET packet_json=? WHERE id=?", (canonical_json(current), trade_id))
        if packet:
            current = json.loads(db.execute("SELECT packet_json FROM trades WHERE id=?", (trade_id,)).fetchone()[0])
            packet(current)
            upsert_packet(db, ClosedTradePacket.model_validate(current))
        for entry_id, change in (fills or {}).items():
            key = json.dumps(["fills", entry_id], separators=(",", ":"))
            raw = json.loads(db.execute("SELECT raw_json FROM source_events WHERE event_id=?", (key,)).fetchone()[0])
            change(raw)
            db.execute("UPDATE source_events SET raw_json=? WHERE event_id=?", (canonical_json(raw), key))
        for column, value in (row or {}).items():
            db.execute(f"UPDATE trades SET {column}=? WHERE id=?", (value, trade_id))


# ---------------------------------------------------------------- finding 2: allocation proof

def test_reviewer_reproduction_close_quantity_larger_than_its_exit_fill(path):
    # Saved fills: entry 4, exits 1 and 1. The first packet claims to close 2 with a 1-contract exit.
    fills = [AVAX_FILLS[0], dict(AVAX_FILLS[1], size="1"), dict(AVAX_FILLS[2], size="1")]
    book(path, fills)
    first, second = trade_ids(path)
    assert views(path)[first]["group_id"] == GROUP              # valid as saved
    rewrite(path, first, packet=lambda p: p.update(closed_quantity="2"))
    shown = views(path)
    assert "not exactly the exit fill's size" in shown[first]["unresolved"]
    assert "failed its proof" in shown[second]["unresolved"]


def test_reviewer_reproduction_exit_price_not_the_saved_exit_fill(path):
    book(path, AVAX_FILLS)
    first, second = trade_ids(path)
    rewrite(path, first, packet=lambda p: p.update(exit_price="999"))
    shown = views(path)
    assert "time or price does not match" in shown[first]["unresolved"] and "unresolved" in shown[second]


def test_row_must_match_its_own_close_evidence(path):
    book(path, AVAX_FILLS)
    first, second = trade_ids(path)
    rewrite(path, first, row={"closed_quantity": "3"})
    shown = views(path)
    assert "row and its own close evidence disagree" in shown[first]["unresolved"] and "unresolved" in shown[second]


@pytest.mark.parametrize("field, value", [("price", "-10.47"), ("price", "0"), ("price", "NaN"), ("price", "Infinity"),
                                          ("size", "0"), ("size", "-4"), ("trade_time", "2026-10-01T12:18:00")])
def test_non_positive_non_finite_or_naive_entry_values_are_refused(path, field, value):
    book(path, AVAX_FILLS)
    first, _ = trade_ids(path)
    for trade_id in trade_ids(path):           # the close's copy and the saved fill agree, but the value is bad
        rewrite(path, trade_id, raw_packet=lambda p: p["source_events"][0]["payload"].update({field: value}))
    rewrite(path, first, fills={"avax-entry": lambda f: f.update({field: value})})
    assert all("unresolved" in v for v in views(path).values())


def test_exit_before_entry_is_refused(path):
    book(path, AVAX_FILLS)
    first, _ = trade_ids(path)
    early = "2026-10-01T12:10:00+00:00"
    rewrite(path, first, raw_packet=lambda p: (p["source_events"][1]["payload"].update(trade_time=early),
                                               p["source_events"][1].update(occurred_at=early), p.update(close_time=early)),
            fills={"avax-exit-1": lambda f: f.update(trade_time=early)}, row={"close_time": early})
    shown = views(path)
    assert "not after the entry fill" in shown[first]["unresolved"]


def test_split_exit_fragments_are_not_grouped(path):
    # One exit fill split over two closes is not a proven allocation.
    book(path, AVAX_FILLS)
    first, _ = trade_ids(path)
    rewrite(path, first, packet=lambda p: p.update(closed_quantity="1"))
    assert "unresolved" in views(path)[first]


def test_actual_avax_pair_still_groups_unchanged(path):
    book(path, AVAX_FILLS)
    shown = views(path)
    assert {v["group_id"] for v in shown.values()} == {GROUP} and len(shown) == 2


# ---------------------------------------------------------------- finding 1: atomic legacy check and write

def conflicted(path):
    book(path, AVAX_FILLS)
    first, second = trade_ids(path)
    with connect(path) as db:
        update_reason(db, first, "old reason", 0)
    return first, second, [{"trade_id": first, "reason_revision": 1}]


def other(path):
    return sqlite3.connect(path, timeout=0.05)


def test_reviewer_reproduction_legacy_edit_just_before_the_shared_helper_is_refused(path, monkeypatch):
    first, second, seen = conflicted(path)
    original = pr.initialize_reasons

    def edit_then_initialize(db):            # the last moment before the reservation is taken
        original(db)
        with other(path) as racer:
            racer.execute("UPDATE trades SET reason='new unseen reason', reason_revision=2 WHERE id=?", (first,))
    monkeypatch.setattr(pr, "initialize_reasons", edit_then_initialize)
    with connect(path) as db, pytest.raises(ValueError, match="legacy reasons changed"):
        pr.save(db, project, GROUP, "old reason", 0, seen)
    monkeypatch.undo()
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_reasons").fetchone()[0] == 0
        assert db.execute("SELECT reason FROM trades WHERE id=?", (first,)).fetchone()[0] == "new unseen reason"


def test_legacy_edit_during_the_reservation_waits_and_cannot_slip_in(path, monkeypatch):
    first, second, seen = conflicted(path)
    original, blocked = pr._legacy, []

    def legacy_with_racer(db, group):         # inside the reservation, before the shared write
        with other(path) as racer:
            try:
                racer.execute("UPDATE trades SET reason='racing edit', reason_revision=2 WHERE id=?", (first,))
                racer.commit()
            except sqlite3.OperationalError as exc:
                blocked.append(str(exc))
        return original(db, group)
    monkeypatch.setattr(pr, "_legacy", legacy_with_racer)
    with connect(path) as db:
        saved = pr.save(db, project, GROUP, "old reason", 0, seen)
    monkeypatch.undo()
    assert blocked and "locked" in blocked[0]
    assert (saved["state"], saved["reason"], saved["revision"]) == ("set", "old reason", 1)
    with other(path) as racer:                # after the commit the racer's edit goes through, as a new legacy revision
        racer.execute("UPDATE trades SET reason='racing edit', reason_revision=2 WHERE id=?", (first,))
    shown = views(path)[first]
    assert shown["reason"] == "old reason" and shown["legacy"][0] == {**shown["legacy"][0], "reason": "racing edit", "reason_revision": 2}


def test_a_sync_during_the_reservation_waits(path, monkeypatch):
    first, second, seen = conflicted(path)
    original, blocked = pr._legacy, []

    def legacy_with_sync(db, group):
        try:
            with sqlite3.connect(path, timeout=0.05) as racer:
                racer.execute("INSERT INTO source_events VALUES ('s','a','[\"fills\",\"late\"]','{}')")
        except sqlite3.OperationalError as exc:
            blocked.append(str(exc))
        return original(db, group)
    monkeypatch.setattr(pr, "_legacy", legacy_with_sync)
    with connect(path) as db:
        pr.save(db, project, GROUP, "old reason", 0, seen)
    assert blocked and "locked" in blocked[0]


def test_a_refused_save_releases_the_reservation(path):
    first, second, seen = conflicted(path)
    with connect(path) as db, pytest.raises(ValueError):
        pr.save(db, project, GROUP, "x", 0, [])
    with other(path) as racer:                # no lock left behind
        racer.execute("UPDATE trades SET reason='after' WHERE id=?", (second,))


def test_open_trade_route_guard_is_inside_the_reservation(path, monkeypatch):
    with connect(path) as db:
        take(db, [], enable=True)
    book(path, [fill("s", "SELL", 4, 40), fill("c1", "BUY", 2, 45)], product=PRODUCT)
    lc = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")
    (row,) = trade_ids(path)
    original = pr.initialize_reasons

    def edit_then_initialize(db):
        original(db)
        with other(path) as racer:
            racer.execute("UPDATE trades SET reason='row reason', reason_revision=1 WHERE id=?", (row,))
    monkeypatch.setattr(pr, "initialize_reasons", edit_then_initialize)
    with connect(path) as db, pytest.raises(pr.Unattributable, match="own reasons"):
        pr.save_lifecycle(db, project, lc, "x", 0)
    monkeypatch.undo()
    with connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM lifecycle_reasons").fetchone()[0] == 0
