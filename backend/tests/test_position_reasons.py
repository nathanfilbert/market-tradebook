"""One entry reason per position, shared by its exit rows; exit reasons, notes and legacy reasons stay per row."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tradebook.api import create_app
from tradebook.domain import ClosedTradePacket
from tradebook.lifecycles import lifecycle_id, project
from tradebook.position_reasons import all_groups, entry_group_id, views_by_trade
from tradebook.store import connect, initialize, update_notes, update_reason, upsert_packet
from test_lifecycles import ACCOUNT, PRODUCT, SOURCE, fill, save
from test_positions import T0, take

AVAX = "AVP-20DEC30-CDE"
# The live case: short 4 at 10.47, covered 2 at 10.37 and 2 at 10.32, before forward tracking.
AVAX_FILLS = [fill("avax-entry", "SELL", 4, 18, price="10.47", product=AVAX),
              fill("avax-exit-1", "BUY", 2, 54, price="10.37", product=AVAX),
              fill("avax-exit-2", "BUY", 2, 57, price="10.32", product=AVAX)]
GROUP = entry_group_id(SOURCE, ACCOUNT, AVAX, "avax-entry")


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "book.sqlite3"
    db = connect(p); initialize(db)
    db.close()
    return p


def book(path, fills, product=AVAX, account=ACCOUNT):
    with connect(path) as db:
        save(db, fills, product=product, account=account)


def api(path, minutes=60):
    return TestClient(create_app(path, clock=lambda: T0 + timedelta(minutes=minutes)))


def trades(path):
    with connect(path) as db:
        return [r[0] for r in db.execute("SELECT id FROM trades ORDER BY close_time")]


def views(path):
    with connect(path) as db:
        return views_by_trade(db, project(db))


def rows(path, sql):
    with connect(path) as db:
        return [tuple(r) for r in db.execute(sql)]


def test_avax_four_to_two_plus_two_shares_one_entry_episode(path):
    book(path, AVAX_FILLS)
    first, second = trades(path)
    shown = views(path)
    assert shown[first] == shown[second]
    group = shown[first]
    assert (group["group_id"], group["kind"], group["opening_entry_id"]) == (GROUP, "entry_fill", "avax-entry")
    assert group["members"] == [first, second] and group["side"] == "short" and group["state"] == "none"
    # Pre-tracking: no lifecycle, no open position claimed, no flat point assumed.
    body = api(path).get("/api/lifecycles").json()
    assert body["items"] == [] and {s["status"] for s in body["scopes"]} == {"unavailable"}


def test_a_lone_partial_close_is_grouped_without_claiming_the_rest_is_open(path):
    book(path, AVAX_FILLS[:2])
    (only,) = trades(path)
    assert views(path)[only]["members"] == [only]
    assert api(path).get("/api/lifecycles?status=open").json()["items"] == []


def test_whole_closes_and_unrelated_episodes_stay_apart(path):
    later = [fill("e2", "SELL", 2, 59, price="10.2", product=AVAX), fill("x3", "BUY", 1, 59, price="10.1", product=AVAX)]
    later[1]["trade_time"] = "2026-10-01T13:05:00Z"
    third = fill("x4", "BUY", 1, 0, price="10.0", product=AVAX)
    third["trade_time"] = "2026-10-01T13:10:00Z"
    whole = [fill("w1", "BUY", 1, 1, product=PRODUCT), fill("w2", "SELL", 1, 2, product=PRODUCT)]
    book(path, AVAX_FILLS + later + [third])
    book(path, whole, product=PRODUCT)
    shown = views(path)
    groups = {v["group_id"]: v["members"] for v in shown.values() if "group_id" in v}
    assert set(groups) == {GROUP, entry_group_id(SOURCE, ACCOUNT, AVAX, "e2")}
    assert all(len(m) == 2 for m in groups.values()) and not set(groups[GROUP]) & set(groups[entry_group_id(SOURCE, ACCOUNT, AVAX, "e2")])
    whole_trade = rows(path, f"SELECT id FROM trades WHERE market='{PRODUCT}'")[0][0]
    assert whole_trade not in shown          # one row closing its whole entry: an ordinary row


def test_accounts_and_products_never_share_a_group(path):
    book(path, AVAX_FILLS)
    book(path, [dict(f, product_id=PRODUCT, entry_id="p-" + f["entry_id"], trade_id="p-" + f["trade_id"]) for f in AVAX_FILLS],
         product=PRODUCT)
    book(path, [dict(f, retail_portfolio_id="other") for f in AVAX_FILLS], account="other")
    ids = {v["group_id"] for v in views(path).values()}
    assert ids == {GROUP, entry_group_id(SOURCE, ACCOUNT, PRODUCT, "p-avax-entry"),
                   entry_group_id(SOURCE, "other", AVAX, "avax-entry")}


def test_ambiguous_add_and_reversal_histories_are_not_grouped(path):
    # Two entry lots closed by one fill: the close cites three fills, so no single entry allocation.
    book(path, [fill("l1", "BUY", 1, 1, product=AVAX), fill("l2", "BUY", 1, 2, product=AVAX),
                fill("c", "SELL", 2, 3, product=AVAX)])
    # A reversal: the closing part closes its whole lot; the residual is not a close at all.
    book(path, [fill("r1", "BUY", 1, 10, product=PRODUCT), fill("rv", "SELL", 3, 11, product=PRODUCT)], product=PRODUCT)
    assert views(path) == {}


def test_incomplete_or_contradicting_evidence_stays_unresolved(path):
    book(path, AVAX_FILLS)
    first, second = trades(path)
    with connect(path) as db:          # the saved exit fill disappears
        db.execute("DELETE FROM source_events WHERE event_id=?", (json.dumps(["fills", "avax-exit-1"], separators=(",", ":")),))
    shown = views(path)
    assert "not saved" in shown[first]["unresolved"] and "failed its proof" in shown[second]["unresolved"]
    with connect(path) as db:          # restore, then make the saved entry differ from what the closes cite
        db.execute("INSERT INTO source_events VALUES (?, ?, ?, ?)", (SOURCE, ACCOUNT, json.dumps(["fills", "avax-exit-1"], separators=(",", ":")),
                                                                   json.dumps(AVAX_FILLS[1], sort_keys=True, separators=(",", ":"))))
        assert views_by_trade(db, project(db))[first]["group_id"] == GROUP
        changed = dict(AVAX_FILLS[0], price="10.48")
        db.execute("UPDATE source_events SET raw_json=? WHERE event_id=?",
                   (json.dumps(changed, sort_keys=True, separators=(",", ":")), json.dumps(["fills", "avax-entry"], separators=(",", ":"))))
    shown = views(path)
    assert all("unresolved" in shown[t] for t in (first, second))


def test_a_second_close_citing_the_same_exit_fill_unresolves_the_group(path):
    book(path, AVAX_FILLS)
    with connect(path) as db:
        packet = json.loads(db.execute("SELECT packet_json FROM trades ORDER BY close_time").fetchone()[0])
        packet["close_id"] = "copy"
        upsert_packet(db, ClosedTradePacket.model_validate(packet))
    shown = views(path)
    assert len(shown) == 3 and all("unresolved" in v for v in shown.values())


@pytest.mark.parametrize("legacy, state, reason", [
    ((None, None), "none", None), (("fade", "fade"), "legacy", "fade"), (("fade", None), "legacy", "fade"),
    (("fade", "chase"), "conflict", None)])
def test_legacy_row_reasons_are_shown_never_chosen(path, legacy, state, reason):
    book(path, AVAX_FILLS)
    with connect(path) as db:
        for trade_id, value in zip(trades(path), legacy):
            if value:
                update_reason(db, trade_id, value, 0)
    group = views(path)[trades(path)[0]]
    assert (group["state"], group["reason"], group["revision"]) == (state, reason, 0)
    assert [r["reason"] for r in group["legacy"]] == list(legacy)


def test_resolving_a_conflict_needs_the_legacy_revisions_shown(path):
    book(path, AVAX_FILLS)
    first, second = trades(path)
    with connect(path) as db:
        update_reason(db, first, "fade", 0)
        update_reason(db, second, "chase", 0)
    client, url = api(path), f"/api/position-reasons/{GROUP}"
    before = rows(path, "SELECT id, reason, reason_revision FROM trades ORDER BY id") + rows(path, "SELECT * FROM reason_history")
    assert client.patch(url, json={"reason": "fade", "expected_revision": 0}).status_code == 409    # not reviewed
    seen = [{"trade_id": first, "reason_revision": 1}, {"trade_id": second, "reason_revision": 1}]
    with connect(path) as db:          # a legacy value changes after the page showed it
        update_reason(db, second, "chase harder", 1)
    assert client.patch(url, json={"reason": "fade", "expected_revision": 0, "legacy_seen": seen}).status_code == 409
    seen[1]["reason_revision"] = 2
    saved = client.patch(url, json={"reason": "  fade the 4h high  ", "expected_revision": 0, "legacy_seen": seen}).json()
    assert (saved["state"], saved["reason"], saved["revision"]) == ("set", "fade the 4h high", 1)
    # The legacy values and their history are untouched and still listed.
    after = rows(path, "SELECT id, reason, reason_revision FROM trades ORDER BY id") + rows(path, "SELECT * FROM reason_history")
    assert [r for r in after if r not in before] == [r for r in after if "chase harder" in r]
    assert [r["reason"] for r in saved["legacy"]] == ["fade", "chase harder"]


def test_edit_from_either_row_updates_both_and_clear_is_an_override(path):
    book(path, AVAX_FILLS)
    first, second = trades(path)
    client, url = api(path), f"/api/position-reasons/{GROUP}"
    assert client.patch(url, json={"reason": "short the retest", "expected_revision": 0}).status_code == 200
    shown = client.get("/api/trades").json()["items"]
    assert {t["position_reason"]["reason"] for t in shown} == {"short the retest"}
    assert client.patch(url, json={"reason": "stale tab", "expected_revision": 0}).status_code == 409
    assert client.patch(url, json={"reason": "  ", "expected_revision": 1}).json()["state"] == "cleared"
    with connect(path) as db:          # a legacy value written later does not come back through a clear
        update_reason(db, first, "late legacy", 0)
    again = client.get(f"/api/trades/{second}").json()["position_reason"]
    assert (again["state"], again["reason"], again["revision"]) == ("cleared", None, 2)
    assert rows(path, f"SELECT revision, reason FROM lifecycle_reason_history WHERE lifecycle_id='{GROUP}' ORDER BY revision") == [
        (1, "short the retest"), (2, None)]
    for _ in range(3):                 # repeated saves with fresh revisions stay ordered
        current = client.get(f"/api/trades/{first}").json()["position_reason"]["revision"]
        assert client.patch(url, json={"reason": f"r{current}", "expected_revision": current}).status_code == 200
    assert client.patch(url, json={"reason": "x", "expected_revision": 1}).status_code == 409
    assert client.patch("/api/position-reasons/unknown", json={"reason": "x", "expected_revision": 0}).status_code == 404
    assert client.patch(url, json={"reason": "x", "expected_revision": 5, "extra": 1}).status_code == 422


def test_exit_reasons_notes_and_shared_reason_stay_separate(path):
    book(path, AVAX_FILLS)
    first, second = trades(path)
    client = api(path)
    with connect(path) as db:
        update_notes(db, first, "tradebook note", 0)
    out = client.patch(f"/api/trades/{first}/exit-reason", json={"exit_reason": " target hit ", "expected_revision": 0}).json()
    assert (out["exit_reason"], out["exit_reason_revision"]) == ("target hit", 1)
    assert client.patch(f"/api/trades/{first}/exit-reason", json={"exit_reason": "x", "expected_revision": 0}).status_code == 409
    assert client.patch(f"/api/trades/{first}/exit-reason", json={"exit_reason": "", "expected_revision": 1}).json()["exit_reason"] is None
    assert client.patch("/api/trades/nope/exit-reason", json={"exit_reason": "x", "expected_revision": 0}).status_code == 404
    client.patch(f"/api/trades/{second}/exit-reason", json={"exit_reason": "stop moved", "expected_revision": 0})
    snapshot = rows(path, "SELECT id, reason, reason_revision, notes, notes_revision, exit_reason, exit_reason_revision FROM trades ORDER BY id")
    client.patch(f"/api/position-reasons/{GROUP}", json={"reason": "shared", "expected_revision": 0})
    assert rows(path, "SELECT id, reason, reason_revision, notes, notes_revision, exit_reason, exit_reason_revision FROM trades ORDER BY id") == snapshot
    assert rows(path, "SELECT trade_id, revision, exit_reason FROM exit_reason_history ORDER BY trade_id, revision") == sorted(
        [(first, 1, "target hit"), (first, 2, None), (second, 1, "stop moved")])


def test_replay_and_reinitialize_are_idempotent_and_keep_every_field(path):
    book(path, AVAX_FILLS)
    client = api(path)
    client.patch(f"/api/position-reasons/{GROUP}", json={"reason": "shared", "expected_revision": 0})
    first = trades(path)[0]
    client.patch(f"/api/trades/{first}/exit-reason", json={"exit_reason": "mine", "expected_revision": 0})
    before = views(path), rows(path, "SELECT * FROM trades ORDER BY id"), rows(path, "SELECT * FROM lifecycle_reasons")
    with connect(path) as db:
        save(db, AVAX_FILLS, product=AVAX)   # the timer's next identical batch
        initialize(db)
        changes = db.total_changes
        views_by_trade(db, project(db))
        assert db.total_changes == changes   # reading groups writes nothing
    assert (views(path), rows(path, "SELECT * FROM trades ORDER BY id"), rows(path, "SELECT * FROM lifecycle_reasons")) == before
    assert client.get("/api/position-reasons").json()["detached_reasons"] == []


def test_tracked_lifecycle_rows_share_the_lifecycle_reason(path):
    with connect(path) as db:
        take(db, [], enable=True)
    fills = [fill("s", "SELL", 4, 40), fill("c1", "BUY", 2, 45), fill("c2", "BUY", 2, 50)]
    book(path, fills, product=PRODUCT)
    lc = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")
    shown = views(path)
    assert {v["group_id"] for v in shown.values()} == {lc} and {v["kind"] for v in shown.values()} == {"lifecycle"}
    client = api(path, 55)
    client.patch(f"/api/position-reasons/{lc}", json={"reason": "fade", "expected_revision": 0})
    assert client.get(f"/api/lifecycles/{lc}").json()["entry_reason"]["reason"] == "fade"


def test_open_trade_route_cannot_skip_legacy_reasons_on_its_exits(path):
    with connect(path) as db:
        take(db, [], enable=True)
    book(path, [fill("s", "SELL", 4, 40), fill("c1", "BUY", 2, 45)], product=PRODUCT)
    lc = lifecycle_id(SOURCE, ACCOUNT, PRODUCT, "s")
    with connect(path) as db:
        update_reason(db, trades(path)[0], "row reason", 0)
    response = api(path, 46).patch(f"/api/lifecycles/{lc}/reason", json={"reason": "x", "expected_revision": 0})
    assert response.status_code == 409 and "own reasons" in response.json()["detail"]


def test_groups_on_a_copy_shaped_like_live_data_need_no_schema_change(path):
    book(path, AVAX_FILLS)
    with connect(path) as db:
        groups, by_trade, unresolved = all_groups(db, project(db))
    assert list(groups) == [GROUP] and not unresolved and len(by_trade) == 2
