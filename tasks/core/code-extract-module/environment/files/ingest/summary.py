from decimal import Decimal

from ingest import utils


def net_total(amounts: list[str]) -> Decimal:
    return sum((utils.parse_amount(a) for a in amounts), Decimal("0"))
