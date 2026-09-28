import pytest

from tradebook.coinbase_fills import CoinbaseFillError, iter_fills

def test_empty_page_with_explicit_continuation_is_not_treated_as_complete():
    class HTTP:
        def get(self, path, params):
            return {"fills": [], "cursor": "next", "has_next": True}
    with pytest.raises(CoinbaseFillError):
        list(iter_fills(HTTP()))

ROUTE = "/api/v3/brokerage/orders/historical/fills"


class FakeHTTP:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        return next(self.pages)


def fill(entry_id, trade_id, trade_type="FILL", sequence_timestamp="2026-01-01T00:00:00Z"):
    return {"entry_id": entry_id, "trade_id": trade_id, "trade_type": trade_type,
            "sequence_timestamp": sequence_timestamp, "trade_time": sequence_timestamp}


def test_paginates_forwards_cursor_and_deduplicates_boundary_entry():
    http = FakeHTTP([
        {"fills": [fill("e1", "t1"), fill("e2", "t2")], "cursor": "c1"},
        {"fills": [fill("e2", "t2"), fill("e3", "t3")], "cursor": "c2"},
        {"fills": [], "cursor": ""},
    ])
    result = list(iter_fills(http, limit=2, product_ids=["BTC-USD"]))
    assert [f["entry_id"] for f in result] == ["e1", "e2", "e3"]
    assert len(http.calls) == 3
    assert http.calls[1][0] == ROUTE
    assert http.calls[1][1]["cursor"] == "c1"
    assert http.calls[0][1]["limit"] == 2
    assert http.calls[0][1]["product_ids"] == ["BTC-USD"]


def test_adjusted_fills_with_same_trade_id_are_retained_by_entry_id():
    http = FakeHTTP([{"fills": [fill("e1", "t1"), fill("e2", "t2", "CORRECTION")]}])
    assert [f["entry_id"] for f in iter_fills(http)] == ["e1", "e2"]


def test_repeated_cursor_with_more_pages_fails_instead_of_looping():
    http = FakeHTTP([
        {"fills": [fill("e1", "t1")], "cursor": "same"},
        {"fills": [fill("e2", "t2")], "cursor": "same"},
    ])
    with pytest.raises(CoinbaseFillError, match="cursor"):
        list(iter_fills(http))


def test_missing_entry_id_fails_closed():
    http = FakeHTTP([{"fills": [{"trade_id": "t1", "trade_type": "FILL", "sequence_timestamp": "x"}]}])
    with pytest.raises(CoinbaseFillError, match="entry_id"):
        list(iter_fills(http))


def test_invalid_page_shape_fails_closed():
    with pytest.raises(CoinbaseFillError):
        list(iter_fills(FakeHTTP([{"fills": "not-list"}])))


def test_conflicting_duplicate_entry_id_fails_closed():
    first = fill("e1", "t1")
    changed = fill("e1", "different")
    with pytest.raises(CoinbaseFillError, match="conflicting"):
        list(iter_fills(FakeHTTP([
            {"fills": [first], "cursor": "next"},
            {"fills": [changed]},
        ])))


def test_empty_page_stops_even_if_cursor_is_present():
    http = FakeHTTP([{"fills": [], "cursor": "unused"}])
    assert list(iter_fills(http)) == []
    assert len(http.calls) == 1


def test_history_cap_fails_instead_of_truncating():
    http = FakeHTTP([
        {"fills": [fill("e1", "t1")], "cursor": "next"},
        {"fills": [fill("e2", "t2")]},
    ])
    with pytest.raises(CoinbaseFillError, match="maximum"):
        list(iter_fills(http, limit=1, max_records=1))


def test_nonempty_full_page_without_cursor_fails_closed():
    with pytest.raises(CoinbaseFillError, match="cursor"):
        list(iter_fills(FakeHTTP([{"fills": [fill("e1", "t1")] }]), limit=1))


@pytest.mark.parametrize("cursor", [None, "", "   "])
def test_full_page_with_blank_cursor_and_has_next_fails_closed(cursor):
    page = {"fills": [fill("e1", "t1")], "cursor": cursor, "has_next": True}
    with pytest.raises(CoinbaseFillError, match="cursor"):
        list(iter_fills(FakeHTTP([page]), limit=1))


def test_has_next_false_with_cursor_fails_closed():
    page = {"fills": [fill("e1", "t1")], "cursor": "next", "has_next": False}
    with pytest.raises(CoinbaseFillError, match="has_next"):
        list(iter_fills(FakeHTTP([page])))


def test_short_page_without_cursor_is_terminal():
    assert [f["entry_id"] for f in iter_fills(
        FakeHTTP([{"fills": [fill("e1", "t1")]}]), limit=2
    )] == ["e1"]


def test_optional_has_next_true_with_cursor_continues():
    http = FakeHTTP([
        {"fills": [fill("e1", "t1")], "cursor": "next", "has_next": True},
        {"fills": [], "cursor": "ignored"},
    ])
    assert [f["entry_id"] for f in iter_fills(http)] == ["e1"]
