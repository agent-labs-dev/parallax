"""Starts Blender on the seeded scene in the box's X11 session.

The desktop box runs every seeded app as `/usr/bin/python3 <path>`; this one builds `set.blend` if the workspace
does not have it yet, then hands the process to Blender, which opens the scene in the box's display. The display is
a frame buffer with no GPU behind it, so Mesa's software renderer draws Blender's window.
"""

import os
import subprocess

os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"

if not os.path.exists("/workspace/set.blend"):
    subprocess.run(
        ["blender", "--background", "--factory-startup", "--python", "/workspace/build_set.py"],
        check=True,
        cwd="/workspace",
    )

os.execvp("blender", ["blender", "--factory-startup", "/workspace/set.blend"])
