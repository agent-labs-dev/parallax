"""Builds the seed ledger, the oracle ledger and the mutant ledgers as GnuCash XML books (gzip, as GnuCash saves).

Run from the task directory: `python solution/build_books.py`. It needs only Python. The seed is what the remote
session opens; the oracle ledger is what a correct run saves; each mutant is that ledger with one thing wrong.
"""

import csv
import gzip
import uuid
from pathlib import Path

HERE = Path(__file__).parent
TASK = HERE.parent
NS = " ".join(
    f'xmlns:{p}="http://www.gnucash.org/XML/{p}"'
    for p in "gnc act book cd cmdty price slot split sx trn ts fs bgt recurrence lot addr".split()
)
CURRENCY = "<cmdty:space>CURRENCY</cmdty:space><cmdty:id>USD</cmdty:id>"
STAMP = "2026-03-01 10:59:00 +0000"
# (path, type): the chart of accounts the remote session opens with.
ACCOUNTS = [
    ("Assets", "ASSET"),
    ("Assets:Checking Account", "BANK"),
    ("Expenses", "EXPENSE"),
    ("Expenses:Office Supplies", "EXPENSE"),
    ("Expenses:Travel", "EXPENSE"),
    ("Expenses:Software", "EXPENSE"),
    ("Expenses:Meals", "EXPENSE"),
    ("Expenses:Utilities", "EXPENSE"),
    ("Equity", "EQUITY"),
    ("Equity:Opening Balances", "EQUITY"),
]
CHECKING = "Assets:Checking Account"
OPENING = ("2026-03-01", "Opening balance", "Equity:Opening Balances", "5000.00")


def guid(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"parallax-remote-ledger/{name}").hex


def cents(amount: str) -> int:
    whole, _, frac = amount.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def account_xml(path: str, kind: str) -> str:
    parent = path.rpartition(":")[0]
    name = path.rpartition(":")[2]
    return (
        f'<gnc:account version="2.0.0"><act:name>{name}</act:name><act:id type="guid">{guid(path)}</act:id>'
        f"<act:type>{kind}</act:type><act:commodity>{CURRENCY}</act:commodity><act:commodity-scu>100</act:commodity-scu>"
        f'<act:parent type="guid">{guid(parent) if parent else guid("root")}</act:parent></gnc:account>'
    )


def split_xml(txn: str, account: str, value: int) -> str:
    return (
        f'<trn:split><split:id type="guid">{guid(txn + account)}</split:id>'
        f"<split:reconciled-state>n</split:reconciled-state><split:value>{value}/100</split:value>"
        f'<split:quantity>{value}/100</split:quantity><split:account type="guid">{guid(account)}</split:account></trn:split>'
    )


def txn_xml(date: str, description: str, other: str, amount: str, *, deposit: bool = False) -> str:
    """A transaction moving `amount` between Checking and `other`: out of Checking unless `deposit`."""
    value = cents(amount) * (1 if deposit else -1)
    key = f"{date}{description}"
    return (
        f'<gnc:transaction version="2.0.0"><trn:id type="guid">{guid(key)}</trn:id><trn:currency>{CURRENCY}</trn:currency>'
        f"<trn:date-posted><ts:date>{date} 10:59:00 +0000</ts:date></trn:date-posted>"
        f"<trn:date-entered><ts:date>{STAMP}</ts:date></trn:date-entered><trn:description>{description}</trn:description>"
        f"<trn:splits>{split_xml(key, CHECKING, value)}{split_xml(key, other, -value)}</trn:splits></gnc:transaction>"
    )


def book(rows: list[tuple[str, str, str, str]]) -> bytes:
    """`rows` are (date, description, expense account, amount); the opening balance is always present."""
    root = (
        f'<gnc:account version="2.0.0"><act:name>Root Account</act:name><act:id type="guid">{guid("root")}</act:id>'
        "<act:type>ROOT</act:type></gnc:account>"
    )
    txns = [txn_xml(OPENING[0], OPENING[1], OPENING[2], OPENING[3], deposit=True)]
    txns += [txn_xml(date, desc, acct, amount) for date, desc, acct, amount in rows]
    body = (
        '<?xml version="1.0" encoding="utf-8" ?>\n'
        f"<gnc-v2 {NS}>\n<gnc:count-data cd:type=\"book\">1</gnc:count-data>\n"
        f'<gnc:book version="2.0.0"><book:id type="guid">{guid("book")}</book:id>\n'
        f'<gnc:count-data cd:type="account">{len(ACCOUNTS) + 1}</gnc:count-data>\n'
        f'<gnc:count-data cd:type="transaction">{len(txns)}</gnc:count-data>\n'
        + root
        + "".join(account_xml(p, k) for p, k in ACCOUNTS)
        + "".join(txns)
        + "</gnc:book>\n</gnc-v2>\n"
    )
    return gzip.compress(body.encode(), mtime=0)


def expected() -> list[tuple[str, str, str, str]]:
    with (TASK / "environment/files/expenses.csv").open() as handle:
        return [(r["date"], r["vendor"], f"Expenses:{r['category']}", r["amount"]) for r in csv.DictReader(handle)]


def write(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(book(rows))


def main() -> None:
    rows = expected()
    write(TASK / "environment/remote/seed.gnucash", [])
    write(HERE / "ledger.gnucash", rows)
    mutants = TASK / "tests/mutants"
    write(mutants / "unsaved.gnucash", [])
    write(mutants / "missing-record.gnucash", rows[:-1])
    write(mutants / "duplicate-record.gnucash", [*rows, rows[1]])
    write(mutants / "wrong-category.gnucash", [rows[0], (*rows[1][:2], "Expenses:Meals", rows[1][3]), *rows[2:]])
    write(mutants / "wrong-amount.gnucash", [*rows[:2], (*rows[2][:3], "98.00"), *rows[3:]])
    write(mutants / "wrong-date.gnucash", [*rows[:3], ("2026-11-03", *rows[3][1:]), rows[4]])
    write(mutants / "swapped-fields.gnucash", [rows[0], (rows[1][0], rows[1][3], rows[1][2], rows[1][1]), *rows[2:]])


if __name__ == "__main__":
    main()
