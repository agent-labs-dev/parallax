"""Starts Blender on the seeded scene with the scene report hook loaded.

The desktop box runs every seeded app as `/usr/bin/python3 <path>`; this one builds `set.blend` if the workspace
does not have it yet, then hands the process to Blender, which opens the scene in the box's X11 session and loads
`scene_report.py` so every save writes down what the scene holds. Nothing else runs: the agent drives Blender's
own window. Software rendering is forced because the box's display is a frame buffer with no GPU behind it.
"""

import os
import subprocess

os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
os.environ.setdefault("BLENDER_EGL", "0")

if not os.path.exists("/workspace/set.blend"):
    subprocess.run(
        ["blender", "--background", "--factory-startup", "--python", "/workspace/build_set.py"],
        check=True,
        cwd="/workspace",
    )

os.execvp("blender", ["blender", "/workspace/set.blend", "--python", "/workspace/scene_report.py"])
