"""Call-time Coinbase CDP key authentication for read-only requests."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


class CoinbaseAuthError(RuntimeError):
    """Sanitized authentication configuration or signing failure."""


def load_coinbase_env(path: str | Path | None = None) -> None:
    """Load only the two Coinbase key variables from a local dotenv file."""
    from dotenv import dotenv_values

    env_path = Path(path) if path is not None else Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    values = dotenv_values(env_path)
    for name in ("COINBASE_API_KEY_NAME", "COINBASE_API_KEY_SECRET"):
        value = values.get(name)
        if value:
            os.environ.setdefault(name, value)


def create_coinbase_authenticator(generator: Callable | None = None):
    """Return a GET-only signer for direct Advanced Trade API calls.

    The CDP signer supports Ed25519; the older Advanced Trade SDK does not.
    """
    def authenticate(method: str, request_path: str) -> str:
        if method != "GET":
            raise CoinbaseAuthError("Coinbase authentication only permits GET requests")
        key_name = os.environ.get("COINBASE_API_KEY_NAME")
        secret = os.environ.get("COINBASE_API_KEY_SECRET")
        if not key_name or not secret:
            raise CoinbaseAuthError("Coinbase authentication is not configured")
        try:
            from cdp.auth.utils.jwt import JwtOptions, generate_jwt
            options = JwtOptions(api_key_id=key_name, api_key_secret=secret,
                                 request_method=method, request_host="api.coinbase.com",
                                 request_path=request_path, audience=["cdp_service"], expires_in=120)
            return (generator or generate_jwt)(options)
        except CoinbaseAuthError:
            raise
        except Exception:
            # Never include dependency exception text; it may contain key material or JWTs.
            raise CoinbaseAuthError("Coinbase authentication failed") from None

    return authenticate
