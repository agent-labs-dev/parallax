import tinyfmt


def csv_amount(cents: int, currency: str) -> str:
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}[currency]
    return tinyfmt.money(cents, symbol=symbol)
