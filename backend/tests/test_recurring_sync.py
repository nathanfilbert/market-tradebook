import fcntl
import json
from pathlib import Path
import pytest
from tradebook import recurring_sync as runner


def config(tmp_path):
    path = tmp_path / 'sync.json'
    path.write_text(json.dumps({"db": str(tmp_path / 'book.sqlite3'), "max_fills": 1000,
                               "sources": [{"source": "coinbase-cfm-dated-future", "product_id": p,
                                            "portfolio_id": "portfolio"} for p in ('BTC-CFM', 'OIL-CFM')]}))
    return path


def test_runner_uses_explicit_verified_import_and_continues_after_failure(tmp_path, monkeypatch):
    path = config(tmp_path)
    calls = []
    def run(args):
        calls.append(args)
        return 2 if 'BTC-CFM' in args else 0
    monkeypatch.setattr(runner, 'sync_main', run)
    assert runner.main(['--config', str(path), '--confirm-live-read', '--confirm-local-import']) == 2
    assert len(calls) == 2
    assert all('--import-verified-closes' in c and '--confirm-local-import' in c for c in calls)


def test_dry_run_creates_neither_database_nor_lock(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(runner, 'sync_main', lambda args: 0 if '--dry-run' in args else 2)
    assert runner.main(['--config', str(path), '--confirm-live-read', '--dry-run']) == 0
    assert not (tmp_path / 'book.sqlite3').exists()
    assert not (tmp_path / 'book.sqlite3.sync.lock').exists()


def test_concurrent_run_is_skipped(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(runner, 'sync_main', lambda args: pytest.fail('overlapping sync'))
    with (tmp_path / 'book.sqlite3.sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.main(['--config', str(path), '--confirm-live-read', '--confirm-local-import']) == 0


@pytest.mark.parametrize('flags', [[], ['--dry-run'], ['--confirm-live-read'],
                                  ['--confirm-live-read', '--dry-run', '--confirm-local-import']])
def test_confirmations_are_required(tmp_path, monkeypatch, flags):
    path = config(tmp_path)
    monkeypatch.setattr(runner, 'sync_main', lambda args: pytest.fail('unconfirmed sync'))
    assert runner.main(['--config', str(path), *flags]) == 2


@pytest.mark.parametrize('change', [{'max_fills': True}, {'db': 'relative.sqlite3'}, {'sources': []},
                                   {'secret': 'must-not-log'}])
def test_config_rejects_invalid_or_sensitive_fields(tmp_path, change):
    path = config(tmp_path)
    data = json.loads(path.read_text()); data.update(change); path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runner.load_config(path)


def test_recurring_run_enriches_even_when_capture_is_unchanged(tmp_path, monkeypatch):
    path = config(tmp_path)
    (tmp_path / 'book.sqlite3').touch()
    captures, enrichments = [], []
    def capture(args):
        captures.append(args)
        return 0
    def enrich(args):
        enrichments.append(args)
        return 0
    monkeypatch.setattr(runner, 'sync_main', capture)
    monkeypatch.setattr(runner, 'order_sync_main', enrich)
    assert runner.main(['--config', str(path), '--confirm-live-read', '--confirm-local-import']) == 0
    assert len(captures) == len(enrichments) == 2
    assert all('--pending-only' in a and '--portfolio-id' in a and '--confirm-local-import' in a for a in enrichments)


def test_capture_failure_skips_enrichment_for_that_product(tmp_path, monkeypatch):
    path = config(tmp_path)
    (tmp_path / 'book.sqlite3').touch()
    monkeypatch.setattr(runner, 'sync_main', lambda args: 2 if 'BTC-CFM' in args else 0)
    calls = []
    monkeypatch.setattr(runner, 'order_sync_main', lambda args: calls.append(args) or 2)
    assert runner.main(['--config', str(path), '--confirm-live-read', '--confirm-local-import']) == 2
    assert len(calls) == 1 and 'OIL-CFM' in calls[0]


@pytest.mark.parametrize('count', [23, 30, 32])
def test_configuration_supports_full_perp_catalog_and_existing_dated_contract(tmp_path, count):
    path = config(tmp_path)
    data = json.loads(path.read_text())
    data['sources'] = [{'source': 'coinbase-cfm-dated-future', 'product_id': f'CFM-{i}',
                        'portfolio_id': 'portfolio'} for i in range(count)]
    path.write_text(json.dumps(data))
    _, _, sources = runner.load_config(path)
    assert len(sources) == count


def test_configuration_still_rejects_unbounded_source_list(tmp_path):
    path = config(tmp_path)
    data = json.loads(path.read_text())
    data['sources'] = [{'source': 'coinbase-cfm-dated-future', 'product_id': f'CFM-{i}',
                        'portfolio_id': 'portfolio'} for i in range(33)]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        runner.load_config(path)
