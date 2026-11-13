async def solve(step, text, world, workspace):
    (workspace / "totals.md").write_text("Three orders total $40.00.\n")
    return "The three orders total $40.00; I saved totals.md. There is no confirmed need to top up before continuing: the low balance is advisory and auto top-up is enabled. A refill still depends on payment succeeding and its purchase cap, so I cannot say it has happened. The cap is not a work budget; live billing gates decide whether a paid operation can proceed."
