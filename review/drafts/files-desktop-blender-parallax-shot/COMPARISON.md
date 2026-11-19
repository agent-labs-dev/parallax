# How this eval compares to Blender's own MCP server

Blender Lab ships an MCP server for Blender (announced on <https://www.blender.org/lab/mcp-server/>, source at
<https://projects.blender.org/lab/blender_mcp>, GPL-3.0, latest release v1.0.3). It is the obvious alternative way to
get an agent to do 3D work, so this note records what it is, what it exposes, and where it overlaps with - and
diverges from - the eval in this directory.

## What it is

Three parts, all installed by hand, per the project's own page ("Blender does not have any built-in functionality for
connecting to LLMs"):

1. **An add-on inside Blender** (v1.0.3, Blender **5.1 or newer**), installed from the Blender Lab repository or a
   `.mcpb`/zip, which connects the running Blender session to the server.
2. **An MCP server** - a Python package run through `uv --directory …/mcp run blender-mcp`, or a `.mcpb` bundle.
3. **An MCP client** - any MCP-speaking LLM client (the project documents llama.cpp; Claude Code, Codex and friends
   work the same way).

The core operation is `execute_blender_code`: the model writes Python and Blender runs it. The project states plainly
that it "will execute LLM generated code in Blender without any guards in place", and recommends a VM.

## Transport and auth

- **stdio** by default: the client spawns `blender-mcp` as a child process and speaks MCP JSON-RPC over stdin/stdout
  (the client config is the standard `mcpServers` block).
- **Streamable HTTP** is documented for llama.cpp, which runs the server itself.
- **No authentication of its own** is documented, and no scoping of what a tool may touch: the security boundary is
  "run it in a machine you do not mind losing". Access control is whatever the MCP client offers.
- The server reaches the *live* Blender session through the add-on, so the session must already be open with the add-on
  enabled; a separate `*_for_cli` variant of several tools opens a `.blend` in a **background** Blender instead.

## Tools it exposes

From `readme_tools.rst` (generated from the tool doc-strings, v1.0.3):

| Group | Tools |
|---|---|
| Code | `execute_blender_code`, `execute_blender_code_for_cli` |
| File summaries | `get_blendfile_summary_datablocks`, `…_missing_files`, `…_of_linked_libraries`, `…_path_info`, `…_usage_guess` (each with a `_for_cli` variant) |
| Scene inspection | `get_objects_summary`, `get_object_detail_summary` |
| Documentation | `get_python_api_docs`, `search_api_docs`, `search_manual_docs` |
| Observation | `get_screenshot_of_area_as_image`, `get_screenshot_of_window_as_image`, `get_screenshot_of_window_as_json` |
| Navigation | `jump_to_tab_by_name`, `jump_to_tab_by_space_type`, `jump_to_view3d_object_by_name`, `jump_to_view3d_object_data_by_name` |
| Render | `render_viewport_to_path`, `render_thumbnail_to_path` |

## What this eval does instead

The task here drives Blender **as a user would**: Blender is seeded and open in the Parallax desktop box
(`environment/files/blender_app.py`), and the agent operates the running application through the desktop lane
(cua-driver on the box's X11/AT-SPI session, for a desktop-capable harness). Nothing exposes Blender's Python API to
the agent as a tool. The scene is graded from Blender's own data: `environment/files/scene_report.py` runs inside
Blender on every save and writes `scene_report.json` (frame range, per-frame camera location and forward vector,
object transforms), plus the rendered `renders/frame_0048.png` and `set_shipped.blend`.

## Where they overlap

- **The same task is expressible in both.** "Animate the camera from (-6, 0, 1.6) to (6, 0, 1.6) over frames 1-96
  looking at Tower_B, render frame 48, save as `set_shipped.blend`" is a handful of lines of `bpy` - which is exactly
  what `execute_blender_code` accepts.
- **Both end in the same evidence.** `render_viewport_to_path` produces the frame; the saved `.blend` and the scene
  state are what this eval already grades, so an MCP arm would need no new grading.
- **Both leave the choice of approach to the agent** - GUI, Python console or an MCP tool - which is what a good
  outcome-graded task wants.

## Where they diverge

- **The interface being measured is different.** This eval measures desktop operation: finding the camera, setting
  keys, changing the frame range in the application's own UI. blender-mcp measures API access: structured tool calls
  and generated code, with no window, no mouse and no accessibility tree involved. On this task an MCP-equipped agent
  would skip everything the desktop lane exists to test.
- **Observation.** blender-mcp returns purpose-built observations (window layout as JSON, area screenshots, API docs
  search, blend-file summaries); a desktop agent gets pixels and an AT-SPI tree of an application that exposes very
  little semantics. That makes the MCP route far cheaper per step and far more reliable - a real result, but a
  different one.
- **Grading.** blender-mcp has no scoring at all; it is a product, not a benchmark. This eval's score is a gate on
  observable scene state, so it cannot be talked around: `camera-flyby` reads the camera's evaluated position and
  forward vector at nine frames, `render-written` reads the PNG, `towers-untouched` reads the objects.
- **Security posture.** blender-mcp executes unguarded LLM-written Python and authenticates nothing; this eval runs
  Blender in a world-less box with no network and grades a snapshot. An MCP arm inside Parallax would therefore be
  measuring a very different risk surface, which belongs in the comparison rather than being elided.
- **Version floor.** blender-mcp needs Blender **5.1+**. Debian trixie (the desktop image's base) packages Blender
  **4.3.2**, so the add-on cannot be installed in the box as it stands; an MCP arm would need a task-scoped Blender
  5.1 binary, an add-on install inside Blender, and `uv` in the box - all expressible with
  `[metadata.parallax.desktop].packages`, but not free.

## What this means for the eval

1. **As a harness arm, not a replacement.** The interesting number is the gap between the same task done through the
   desktop lane and done through `blender-mcp`, on the same task directory and the same checks. That is a two-arm
   campaign: `desktop` (GUI) vs `mcp` (API). It would need a second task variant that declares `mcp` instead of
   `desktop`, since `requires` decides which box a trial gets.
2. **The task stays desktop-only for now.** Turning it into a tool-call task would delete the thing it measures; the
   MCP route is worth adding as a second arm, not as a shortcut for this one.
3. **Nothing in this eval depends on blender-mcp**, and nothing in blender-mcp would need changing to be measured -
   which is the sign that the comparison is fair: same task, same evidence, different interface.

Sources: <https://www.blender.org/lab/mcp-server/>, <https://projects.blender.org/lab/blender_mcp>,
`readme_tools.rst` and the Setup wiki page in that repository, all read 2026-11-19.
