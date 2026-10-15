"""Explicit server-owned catalog. It never reads provider credentials or accepts client prices."""

import json
import os
import re
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Plan:
    price_id: str
    unit_amount: int
    currency: str
    interval: str


def load_catalog(supplied=None):
    if supplied is None:
        raw = os.environ.get("PAYMENT_PLAN_CATALOG_JSON")
        if not raw:
            return MappingProxyType({})
        if len(raw) > 65536:
            raise ValueError("catalog too large")
        try:
            supplied = json.loads(raw)
        except Exception:
            raise ValueError("invalid plan catalog") from None
    if not isinstance(supplied, dict) or len(supplied) > 100:
        raise ValueError("invalid plan catalog")
    result = {}
    for name, value in supplied.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name)
            or not isinstance(value, dict)
            or set(value) != {"price_id", "unit_amount", "currency", "interval"}
        ):
            raise ValueError("invalid plan")
        if not isinstance(value["price_id"], str) or not re.fullmatch(
            r"price_[A-Za-z0-9_]{1,240}", value["price_id"]
        ):
            raise ValueError("invalid catalog price")
        if (
            type(value["unit_amount"]) is not int
            or not 1 <= value["unit_amount"] <= 999999999
        ):
            raise ValueError("invalid catalog amount")
        if value["currency"] not in {
            "usd",
            "eur",
            "gbp",
            "thb",
            "jpy",
            "aud",
            "cad",
            "sgd",
        } or value["interval"] not in {"month", "year"}:
            raise ValueError("invalid catalog currency or interval")
        result[name] = Plan(**value)
    return MappingProxyType(result)
