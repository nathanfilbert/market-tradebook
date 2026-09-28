from dataclasses import FrozenInstanceError

import pytest

from tradebook.coinbase_products import UnsupportedProduct, classify_product


def spot():
    return {
        "product_id": "BTC-USD",
        "product_type": "SPOT",
        "base_currency_id": "BTC",
        "quote_currency_id": "USD",
        "future_product_details": None,
    }


def future(expiry_type="PERPETUAL"):
    return {
        "product_id": "BTC-CFM",
        "product_type": "FUTURE",
        "base_currency_id": "BTC",
        "quote_currency_id": "USD",
        "future_product_details": {
            "contract_expiry_type": expiry_type,
            "contract_size": "0.001",
            "risk_managed_by": "MANAGED_BY_FCM",
        },
    }


def test_spot_product_has_immutable_spot_spec():
    spec = classify_product(spot(), account_family="SPOT", source_reference="fixture:spot")

    assert (spec.product_type, spec.account_family, spec.quantity_unit) == ("spot", "spot", "BTC")
    assert spec.quote_currency == "USD"
    assert spec.contract_multiplier is None
    with pytest.raises(FrozenInstanceError):
        spec.product_id = "changed"


def test_cfm_expiry_metadata_classifies_perpetual_or_dated_future():
    perp = classify_product(future(), account_family="CFM", source_reference="fixture:cfm")
    dated = classify_product(future("EXPIRING"), account_family="CFM", source_reference="fixture:cfm")

    assert perp.product_type == "perpetual"
    assert dated.product_type == "dated_future"
    assert perp.contract_multiplier is None  # no verified unit for contract_size


@pytest.mark.parametrize("family", ["INTX", "UNKNOWN"])
def test_non_cfm_futures_fail_closed(family):
    with pytest.raises(UnsupportedProduct):
        classify_product(future(), account_family=family, source_reference="fixture:ambiguous")


def test_unknown_future_expiry_and_quote_convention_fail_closed():
    with pytest.raises(UnsupportedProduct):
        classify_product(future("MYSTERY"), account_family="CFM", source_reference="fixture:cfm")
    ambiguous = future()
    ambiguous["quote_currency_id"] = None
    with pytest.raises(UnsupportedProduct):
        classify_product(ambiguous, account_family="CFM", source_reference="fixture:cfm")


def test_spot_requires_quote_currency():
    product = spot()
    product["quote_currency_id"] = ""
    with pytest.raises(UnsupportedProduct):
        classify_product(product, account_family="SPOT", source_reference="fixture:spot")

def test_venue_managed_perpetual_is_not_us_cfm_even_if_caller_says_cfm():
    product = future()
    product["future_product_details"]["risk_managed_by"] = "MANAGED_BY_VENUE"
    with pytest.raises(UnsupportedProduct):
        classify_product(product, account_family="CFM", source_reference="fixture")
