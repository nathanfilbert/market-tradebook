from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal


class UnsupportedProduct(ValueError):
    """Product metadata is insufficient or contradictory; do not derive trades."""


@dataclass(frozen=True, slots=True)
class ProductSpecification:
    product_id: str
    product_type: Literal["spot", "perpetual", "dated_future"]
    quote_currency: str
    quantity_unit: str
    contract_multiplier: Decimal | None
    multiplier_unit: str | None
    account_family: Literal["spot", "cfm"]
    source_reference: str


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedProduct(f"missing or invalid {field}")
    return value.strip()


def classify_product(
    product: dict,
    *,
    account_family: str,
    source_reference: str,
) -> ProductSpecification:
    """Classify from product/account metadata; never infer INTX as US CFM."""
    if not isinstance(product, dict):
        raise UnsupportedProduct("product metadata must be an object")
    product_id = _required_text(product.get("product_id"), "product_id")
    quote = _required_text(product.get("quote_currency_id"), "quote_currency_id")
    reference = _required_text(source_reference, "source_reference")

    if product.get("product_type") == "SPOT":
        if account_family != "SPOT":
            raise UnsupportedProduct("spot product requires explicit SPOT account family")
        base = _required_text(product.get("base_currency_id"), "base_currency_id")
        return ProductSpecification(product_id, "spot", quote, base, None, None, "spot", reference)

    if product.get("product_type") != "FUTURE" or account_family != "CFM":
        raise UnsupportedProduct("unsupported or ambiguous product/account family")

    details = product.get("future_product_details")
    if not isinstance(details, dict):
        raise UnsupportedProduct("missing future_product_details")
    if details.get("risk_managed_by") != "MANAGED_BY_FCM":
        raise UnsupportedProduct("future is not verified as CFM-managed")
    expiry = details.get("contract_expiry_type")
    if expiry == "PERPETUAL":
        kind: Literal["perpetual", "dated_future"] = "perpetual"
    elif expiry == "EXPIRING":
        kind = "dated_future"
    else:
        raise UnsupportedProduct("unknown contract_expiry_type")

    # contract_size has no physical/quote unit in the supplied metadata, so
    # preserving the value as a multiplier would imply unverified economics.
    return ProductSpecification(product_id, kind, quote, "contracts", None, None, "cfm", reference)
