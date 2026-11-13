# tinyfmt changelog

## 2.0.0

Breaking:
- `money(cents, symbol="$")` is now `format_money(amount_cents, *, currency="USD")`. Pass an ISO currency
  code instead of a symbol; USD, GBP, EUR and JPY are supported.
- `pct(fraction)` is now `format_percent(fraction, *, digits=0)`. Note the default precision dropped from
  one decimal place to none; pass `digits=1` to keep 1.x output.
- The 1.x names are removed.

## 1.4.0

- `money` accepts a `symbol`.
