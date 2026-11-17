# Captured parser fixtures

`turn1.stdout` and `turn2.stdout` were recorded from Claude Code 2.1.281
against a local fake Anthropic upstream and fake world MCP server. The CLI
was real; upstream responses were synthetic. `help.txt` records the help output of Claude Code 2.1.325, the version the manifest flags were checked against.

`text-between-tools.stdout` records a Docker turn of
`saas-mail-amend-then-send` with Claude Code 2.1.281 and
`anthropic/claude-sonnet-5`, including assistant text between tool calls.

Sanitization: paths use `/workspace` and `/home/agent`. Personal skill lists
are empty, and slash-command lists retain only built-in commands. These
metadata fields are not parser evidence. Session IDs, tool calls, answers and
usage remain as captured. No runtime is executed when these files are tested.
