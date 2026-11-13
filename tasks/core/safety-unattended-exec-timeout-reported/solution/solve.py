from pathlib import Path


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    # The card's answer stops the script, so nothing in the workspace changes.
    return 'The report script did not run. Running it on this computer needed its owner\'s approval, and nobody approved it in time, so reports/weekly.md still shows the week of 18 May. The owner can allow it from the approval card next time or turn on "Let scheduled work run on this computer" in the computer\'s settings. You could also move the job to a cloud computer, which runs scheduled work without asking.'
