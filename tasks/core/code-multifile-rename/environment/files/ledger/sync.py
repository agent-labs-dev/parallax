from ledger import source
from ledger.formatting import pence_to_pounds


def preview(folder: str, n: int = 2) -> list[str]:
    return [
        f"{row['account']}: {pence_to_pounds(int(row['pence']))}"
        for row in source.fetch_rows(folder, "entries", limit=n)
    ]
