import pytest
import base64
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tradebook.coinbase_auth import CoinbaseAuthError, create_coinbase_authenticator, load_coinbase_env

def test_direct_advanced_trade_get_uses_ed25519_jwt(monkeypatch):
    key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "organizations/test/apiKeys/test")
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", base64.b64encode(seed + public).decode())
    monkeypatch.delenv("COINBASE_API_PRIVATE_KEY", raising=False)
    token = create_coinbase_authenticator()("GET", "/api/v3/brokerage/orders/historical/fills?limit=1")
    claims = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], audience="cdp_service")
    assert jwt.get_unverified_header(token)["alg"] == "EdDSA"
    assert claims["uris"] == ["GET api.coinbase.com/api/v3/brokerage/orders/historical/fills"]


def test_authenticator_uses_cdp_generator_with_exact_get_uri_and_call_time_env(monkeypatch):
    calls = []
    key_name = "organizations/example/apiKeys/key-id"
    secret = "synthetic"

    def generate(options):
        calls.append(options)
        return "signed-token"

    auth = create_coinbase_authenticator(generate)
    monkeypatch.setenv("COINBASE_API_KEY_NAME", key_name)
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", secret)

    assert auth("GET", "/api/v3/brokerage/products?limit=2&product_ids=BTC-USD") == "signed-token"
    assert len(calls) == 1
    assert calls[0].api_key_id == key_name and calls[0].api_key_secret == secret
    assert calls[0].request_method == "GET" and calls[0].request_host == "api.coinbase.com"
    assert calls[0].request_path == "/api/v3/brokerage/products?limit=2&product_ids=BTC-USD"
    assert calls[0].audience == ["cdp_service"]


def test_missing_credentials_fail_closed_without_calling_helpers(monkeypatch):
    monkeypatch.delenv("COINBASE_API_KEY_NAME", raising=False)
    monkeypatch.delenv("COINBASE_API_KEY_SECRET", raising=False)
    calls = []
    auth = create_coinbase_authenticator(lambda options: calls.append(options))

    with pytest.raises(CoinbaseAuthError) as exc:
        auth("GET", "/api/v3/brokerage/products")
    assert str(exc.value) == "Coinbase authentication is not configured"
    assert calls == []


def test_invalid_method_is_rejected_and_secrets_are_redacted(monkeypatch):
    key_name = "SECRET-KEY-NAME"
    secret = "SECRET-PRIVATE-KEY"
    monkeypatch.setenv("COINBASE_API_KEY_NAME", key_name)
    monkeypatch.setenv("COINBASE_API_KEY_SECRET", secret)

    def fail(*args):
        raise RuntimeError(f"{key_name} {secret} token-secret")

    auth = create_coinbase_authenticator(fail)
    with pytest.raises(CoinbaseAuthError) as exc:
        auth("POST", "/api/v3/brokerage/orders")
    assert str(exc.value) == "Coinbase authentication only permits GET requests"

    with pytest.raises(CoinbaseAuthError) as exc:
        auth("GET", "/api/v3/brokerage/products")
    assert str(exc.value) == "Coinbase authentication failed"
    assert key_name not in str(exc.value)
    assert secret not in str(exc.value)
    assert "token-secret" not in str(exc.value)

def test_project_env_loads_requested_secret_without_overriding_process_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('COINBASE_API_KEY_NAME="organizations/file/apiKeys/id"\nCOINBASE_API_KEY_SECRET="fake-base64"\n')
    monkeypatch.setenv("COINBASE_API_KEY_NAME", "organizations/process/apiKeys/id")
    monkeypatch.delenv("COINBASE_API_KEY_SECRET", raising=False)
    load_coinbase_env(path)
    assert __import__("os").environ["COINBASE_API_KEY_NAME"] == "organizations/process/apiKeys/id"
    assert __import__("os").environ["COINBASE_API_KEY_SECRET"] == "fake-base64"
