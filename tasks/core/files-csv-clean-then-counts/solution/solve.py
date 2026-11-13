import csv
from collections import Counter
from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    with (workspace / "leads.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    seen: set[str] = set()
    kept: list[dict[str, str]] = []
    for row in rows:
        email = row["email"].strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        kept.append({**row, "email": email, "source": row["source"].strip().capitalize()})
    with (workspace / "cleaned.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "email", "source", "signed_up"])
        writer.writeheader()
        writer.writerows(kept)
    counts = Counter(r["source"] for r in kept)
    lines = [f"- {source}: {n}" for source, n in counts.most_common()]
    return f"Saved cleaned.csv with {len(kept)} leads. By source:\n" + "\n".join(lines)
