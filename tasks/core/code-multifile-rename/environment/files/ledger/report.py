from ledger.source import fetch_rows


def total_by_account(folder: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in fetch_rows(folder, table="entries"):
        totals[row["account"]] = totals.get(row["account"], 0) + int(row["pence"])
    return totals
