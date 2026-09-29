"""Recipes for stripe-python, for ``StripeClient`` services (``create(params, options)``,
``delete(customer, params, options)``) and the older ``stripe.Transfer.create(**params)`` style alike.

Field names follow https://docs.stripe.com/api/transfers/create, /api/payouts/create,
/api/refunds/create and /api/customers/delete. Only ids, amounts in minor units, enums,
flags and a capped description are shown; never metadata or emails. Fields the caller
left out, or passed as ``None`` or ``""``, don't appear. Every Outis SDK shows the same
fields for the same call, pinned by ``recipe-vectors.json``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional, Union

from ._recipe import Recipe

DESCRIPTION_CAP = 64

_Value = Union[str, int, bool, None]


def _params(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(kwargs.get("params"), Mapping):
        return kwargs["params"]  # type: ignore[no-any-return]
    if args and isinstance(args[0], Mapping):
        return args[0]
    return kwargs


def _account(args: tuple[Any, ...], kwargs: Mapping[str, Any], at: int) -> Optional[str]:
    """The connected account, from ``options`` (keyword or positional ``at``) or a legacy ``stripe_account=``."""
    options = kwargs.get("options", args[at] if len(args) > at else None)
    if isinstance(options, Mapping) and options.get("stripe_account") is not None:
        return _text(options["stripe_account"], "stripe_account")
    return _text(kwargs.get("stripe_account"), "stripe_account")


def _minor(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"stripe {field} must be an integer in minor units")
    return value


def _text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"stripe {field} must be a string")
    return value


def _flag(value: Any, field: str) -> Optional[bool]:
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"stripe {field} must be a boolean")
    return value


def _required(value: Any, field: str, method: str) -> Any:
    if value is None or value == "":
        raise ValueError(f"stripe {method} needs {field}")
    return value


def _shown(fields: Mapping[str, _Value]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if key == "description" and len(text) > DESCRIPTION_CAP:
            text = text[: DESCRIPTION_CAP - 3] + "..."
        out[key] = text
    return out


def _transfer(*args: Any, **kwargs: Any) -> dict[str, str]:
    p = _params(args, kwargs)
    m = "transfers.create"
    return _shown({
        "amount": _required(_minor(p.get("amount"), "amount"), "amount", m),
        "currency": _required(_text(p.get("currency"), "currency"), "currency", m),
        "destination": _required(_text(p.get("destination"), "destination"), "destination", m),
        "source_transaction": _text(p.get("source_transaction"), "source_transaction"),
        "description": _text(p.get("description"), "description"),
        "stripe_account": _account(args, kwargs, 1),
    })


def _payout(*args: Any, **kwargs: Any) -> dict[str, str]:
    p = _params(args, kwargs)
    m = "payouts.create"
    return _shown({
        "amount": _required(_minor(p.get("amount"), "amount"), "amount", m),
        "currency": _required(_text(p.get("currency"), "currency"), "currency", m),
        "destination": _text(p.get("destination"), "destination"),
        "method": _text(p.get("method"), "method"),
        "source_type": _text(p.get("source_type"), "source_type"),
        "description": _text(p.get("description"), "description"),
        "stripe_account": _account(args, kwargs, 1),
    })


def _refund(*args: Any, **kwargs: Any) -> dict[str, str]:
    p = _params(args, kwargs)
    charge = _text(p.get("charge"), "charge")
    payment_intent = _text(p.get("payment_intent"), "payment_intent")
    if not charge and not payment_intent:
        raise ValueError("stripe refunds.create needs charge or payment_intent")
    amount = _minor(p.get("amount"), "amount")
    return _shown({
        "charge": charge,
        "payment_intent": payment_intent,
        # Stripe refunds whatever remains on the charge when amount is left out.
        "amount": "full" if amount is None else amount,
        "reason": _text(p.get("reason"), "reason"),
        "reverse_transfer": _flag(p.get("reverse_transfer"), "reverse_transfer"),
        "refund_application_fee": _flag(p.get("refund_application_fee"), "refund_application_fee"),
        "stripe_account": _account(args, kwargs, 1),
    })


def _customer_delete(*args: Any, **kwargs: Any) -> dict[str, str]:
    customer = args[0] if args else kwargs.get("customer", kwargs.get("sid"))
    return _shown({
        "customer": _required(_text(customer, "customer"), "customer", "customers.delete"),
        "stripe_account": _account(args, kwargs, 2),
    })


class _Transfers:
    def create(self) -> Recipe:
        """``transfers.create``: amount, currency, destination, source_transaction, a capped description
        and the connected account."""
        return {"action": "stripe.transfers.create", "show_approvers": _transfer, "call": "transfers.create"}


class _Payouts:
    def create(self) -> Recipe:
        """``payouts.create``: amount, currency, destination, method, source_type, a capped description
        and the connected account, each only when passed."""
        return {"action": "stripe.payouts.create", "show_approvers": _payout, "call": "payouts.create"}


class _Refunds:
    def create(self) -> Recipe:
        """``refunds.create``: the charge or payment intent, amount (``full`` when omitted), reason,
        reverse_transfer, refund_application_fee and the connected account."""
        return {"action": "stripe.refunds.create", "show_approvers": _refund, "call": "refunds.create"}


class _Customers:
    def delete(self) -> Recipe:
        """``customers.delete``: the customer id and the connected account."""
        return {"action": "stripe.customers.delete", "show_approvers": _customer_delete, "call": "customers.delete"}


transfers = _Transfers()
payouts = _Payouts()
refunds = _Refunds()
customers = _Customers()

__all__ = ["DESCRIPTION_CAP", "customers", "payouts", "refunds", "transfers"]
