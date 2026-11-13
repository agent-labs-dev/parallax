Recorded from openclaw 2026.9.2 in the docker box (`parallax run --isolation docker`, model
anthropic/claude-haiku-4-5 through the meter), stdout of the manifest's turn command: the
`openclaw agent --local --json` envelope, the events marker, then the transcript lines of
`openclaw sessions export-trajectory`.

- turn1.stdout: conf-mcp-echo, turn 1 (text, two world tool calls, closing text).
- resume.stdout: conf-two-turn-carry, turn 2 on the same `--session-id`; the export also holds
  turn 1's events, which the parser must skip.
- error.stdout: the envelope openclaw printed when the meter answered 404 for the model.

Container paths only (/workspace, /home/agent); no host paths, tokens or keys.
