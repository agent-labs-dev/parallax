from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ingest.utils import parse_amount, parse_date


@dataclass(frozen=True)
class Line:
    when: date
    payee: str
    amount: Decimal


def load_line(raw: str) -> Line:
    when, payee, amount = raw.split("|")
    return Line(parse_date(when), payee.strip(), parse_amount(amount))
