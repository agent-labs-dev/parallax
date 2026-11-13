from pathlib import Path

FOLDERS = {
    ".jpg": "Images", ".png": "Images",
    ".pdf": "Documents", ".docx": "Documents", ".txt": "Documents",
    ".xlsx": "Spreadsheets", ".csv": "Spreadsheets",
    ".mp3": "Audio", ".m4a": "Audio",
    ".zip": "Archives",
    ".mp4": "Video",
}  # fmt: skip


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    downloads = workspace / "Downloads"
    moved = 0
    for path in sorted(p for p in downloads.iterdir() if p.is_file()):
        target = downloads / FOLDERS[path.suffix.lower()]
        target.mkdir(exist_ok=True)
        path.rename(target / path.name)
        moved += 1
    folders = ", ".join(sorted(set(FOLDERS.values())))
    return f"Moved all {moved} files into subfolders of Downloads: {folders}."
