"""Field validation for the UK payout form."""

import re

_SORT_CODE = re.compile(r"^\d{2}-?\d{2}-?\d{2}$")


def validate_sort_code(value: str) -> bool:
    return bool(_SORT_CODE.match(value.strip()))


def validate_account_number(value: str) -> bool:
    digits = value.strip()
    return digits.isdigit() and len(digits) == 8


def validate_payee_name(value: str) -> bool:
    name = value.strip()
    return 0 < len(name) <= 18
