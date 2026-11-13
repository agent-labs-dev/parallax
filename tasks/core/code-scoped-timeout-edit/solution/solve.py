from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    billing = workspace / "clients" / "billing.py"
    billing.write_text(billing.read_text().replace("TIMEOUT_SECONDS = 10", "TIMEOUT_SECONDS = 30"))
    return "Raised TIMEOUT_SECONDS in clients/billing.py from 10 to 30. Shipping and inventory stay at 10."
