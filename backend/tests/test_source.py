# backend/tests/test_source.py
from tradebook.source import TradeSource


def test_interface_requires_a_source_key_and_packets():
    class Good:
        source_key = "mock.spot"
        def iter_closes(self): return iter(())
    class Bad:
        source_key = "mock.spot"
    assert isinstance(Good(), TradeSource)
    assert not isinstance(Bad(), TradeSource)
