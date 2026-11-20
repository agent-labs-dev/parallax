"""The oracle: the workspace a correct fly-by leaves behind, built without a desktop.

`set_shipped.blend` and `frame_0048.png` beside this file are a real Blender save and render of the correct
fly-by, made by `build_scenes.py` in the task's verify image. The oracle copies them in and leaves the seeded
scene and scripts as they are.
"""

import shutil
from pathlib import Path

HERE = Path(__file__).parent


async def solve(step: str, text: str, world: object, workspace: Path) -> str:
    shutil.copyfile(HERE / "set_shipped.blend", workspace / "set_shipped.blend")
    renders = workspace / "renders"
    renders.mkdir(exist_ok=True)
    shutil.copyfile(HERE / "frame_0048.png", renders / "frame_0048.png")
    return "Animated the camera fly-by, rendered frame 48 and saved set_shipped.blend."
