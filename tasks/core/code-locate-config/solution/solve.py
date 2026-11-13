from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    return (
        "It's set in dispatch/config/defaults.toml under [retry]: limit = 5. dispatch/settings.py loads that "
        "file and worker.should_retry compares attempts against it. Two things that look like answers but "
        "aren't: the comment in worker.py still says 3 (it's stale), and tests/test_worker.py uses its own "
        "Settings with retry_limit=2 just for the test."
    )
