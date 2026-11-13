"""Payments: customers, charges and refunds. Amounts are in major units (300.0 = 300.00).

Tables:
- customers: {id, name, email}
- charges:   {id, customer, amount, currency, description, status, amount_refunded, idempotency_key, created}
- refunds:   {id, charge, amount, currency, reason, created}

`create_charge` honours an idempotency key: a retry with the same key and the same parameters
returns the original charge instead of charging twice (what a careful agent does after a timeout).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar

from pydantic import AfterValidator, Field, JsonValue

from parallax.models import JsonObject
from parallax.world.service import Service, ServiceError, tool
from parallax.world.services.common import (
    Args,
    get_num,
    get_str,
    json_list,
    now_iso,
    opt_str,
    require,
    resolve,
    text_hit,
)


def _cents(value: float) -> float:
    if round(value, 2) != value:
        raise ValueError("at most two decimal places")
    return value


Amount = Annotated[float, Field(gt=0, description="Amount in major units, e.g. 300 or 12.50"), AfterValidator(_cents)]
Currency = Annotated[
    str, Field(pattern=r"^[A-Za-z]{3}$", description="ISO currency code, e.g. GBP"), AfterValidator(str.upper)
]


class ChargeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


class CustomersArgs(Args):
    query: str | None = Field(None, description="Words that must appear in the name or email")


class ChargeArgs(Args):
    customer: str = Field(description="Customer id, name or email")
    amount: Amount
    currency: Currency
    description: str = ""
    idempotency_key: str | None = Field(None, description="Reuse the same key when retrying so the charge happens once")


class ListChargesArgs(Args):
    customer: str | None = Field(None, description="Customer id, name or email")


class IdArgs(Args):
    id: str = Field(description="Charge id")


class RefundArgs(Args):
    charge: str = Field(description="Charge id")
    amount: Amount | None = Field(None, description="Defaults to the whole unrefunded amount")
    reason: str = ""


class ListRefundsArgs(Args):
    charge: str | None = Field(None, description="Only refunds of this charge")


class PaymentsService(Service):
    name: ClassVar[str] = "payments"
    tables: ClassVar[tuple[str, ...]] = ("customers", "charges", "refunds")

    @tool(mutates=False, description="Find customers.")
    async def list_customers(self, args: CustomersArgs) -> JsonValue:
        rows = [
            c for c in self.world.table("payments.customers").values() if text_hit(c, ("name", "email"), args.query)
        ]
        return {"customers": json_list(rows)}

    @tool(
        mutates=True,
        additive=True,
        description="Charge a customer. Pass an idempotency_key so a retry cannot charge twice.",
    )
    async def create_charge(self, args: ChargeArgs) -> JsonValue:
        customer = get_str(self._customer(args.customer), "id")
        if args.idempotency_key is not None:
            prior = self._by_key(args.idempotency_key)
            if prior is not None:
                return self._replay(prior, customer, args)
        row: JsonObject = {
            "id": self.world.new_id("ch_"),
            "customer": customer,
            "amount": args.amount,
            "currency": args.currency,
            "description": args.description,
            "status": ChargeStatus.SUCCEEDED.value,
            "amount_refunded": 0.0,
            "idempotency_key": args.idempotency_key,
            "created": now_iso(self.world),
        }
        self.world.table("payments.charges")[get_str(row, "id")] = row
        return row

    @tool(mutates=False, description="List charges, newest first.")
    async def list_charges(self, args: ListChargesArgs) -> JsonValue:
        customer = get_str(self._customer(args.customer), "id") if args.customer else None
        rows = [
            c
            for c in self.world.table("payments.charges").values()
            if customer is None or c.get("customer") == customer
        ]
        rows.sort(key=lambda c: get_str(c, "created"), reverse=True)
        return {"charges": json_list(rows)}

    @tool(mutates=False, description="Get one charge.")
    async def get_charge(self, args: IdArgs) -> JsonValue:
        return require(self.world, "payments.charges", args.id, "charge")

    @tool(mutates=True, description="Refund a charge, fully or partly. Moves money back to the customer.")
    async def create_refund(self, args: RefundArgs) -> JsonValue:
        charge = require(self.world, "payments.charges", args.charge, "charge")
        remaining = round(get_num(charge, "amount") - get_num(charge, "amount_refunded"), 2)
        amount = args.amount if args.amount is not None else remaining
        if remaining <= 0:
            raise ServiceError("conflict", f"charge {args.charge} is already fully refunded")
        if amount > remaining:
            raise ServiceError("conflict", f"refund {amount} exceeds the unrefunded {remaining}")
        refunded = round(get_num(charge, "amount_refunded") + amount, 2)
        status = ChargeStatus.REFUNDED if refunded >= get_num(charge, "amount") else ChargeStatus.PARTIALLY_REFUNDED
        self.world.table("payments.charges")[args.charge] = {
            **charge,
            "amount_refunded": refunded,
            "status": status.value,
        }
        row: JsonObject = {
            "id": self.world.new_id("re_"),
            "charge": args.charge,
            "amount": amount,
            "currency": charge.get("currency"),
            "reason": args.reason,
            "created": now_iso(self.world),
        }
        self.world.table("payments.refunds")[get_str(row, "id")] = row
        return row

    @tool(mutates=False, description="List refunds.")
    async def list_refunds(self, args: ListRefundsArgs) -> JsonValue:
        rows = [r for r in self.world.table("payments.refunds").values() if args.charge in (None, r.get("charge"))]
        return {"refunds": json_list(rows)}

    def _customer(self, ref: str) -> JsonObject:
        return resolve(self.world, "payments.customers", ref, "customer", keys=("name", "email"))

    def _by_key(self, key: str) -> JsonObject | None:
        hits = [c for c in self.world.table("payments.charges").values() if opt_str(c, "idempotency_key") == key]
        return hits[0] if hits else None

    def _replay(self, prior: JsonObject, customer: str, args: ChargeArgs) -> JsonObject:
        same = (prior.get("customer"), prior.get("amount"), prior.get("currency")) == (
            customer,
            args.amount,
            args.currency,
        )
        if not same:
            raise ServiceError("conflict", "idempotency_key was already used with different parameters")
        return prior
