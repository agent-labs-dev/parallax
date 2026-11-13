"""The runner's internal world processes: `python -m parallax.world serve|bridge`.

`serve --http` is the multi-tenant world server a run starts in its sidecar; worlds register
through its admin API. `serve --task DIR` serves one task's world alone (over stdio without
--http), for authoring. `bridge` relays a harness's stdio MCP session to a server over HTTP.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from parallax.meter import parse_http
from parallax.task import load_task
from parallax.world.core import World
from parallax.world.server import ADMIN_TOKEN_ENV, WorldServer, bridge_main, serve_http, serve_stdio


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m parallax.world")
    sub = parser.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--http", metavar="HOST:PORT")
    serve.add_argument("--task", type=Path, metavar="DIR", help="serve this task's world alone")
    bridge = sub.add_parser("bridge")
    bridge.add_argument("--url", required=True)
    bridge.add_argument("--token-env", required=True, metavar="VAR")
    args = parser.parse_args()
    if args.action == "bridge":
        return bridge_main(args.url, args.token_env)
    if args.task is not None:
        world = World.for_task(load_task(args.task))
        if args.http is None:
            asyncio.run(serve_stdio(world))
            return 0
        server = WorldServer()
        token, control = server.register(world)
        print(f"MCP: http://{args.http}/mcp\ntoken: {token}\ncontrol token: {control}", file=sys.stderr, flush=True)
        asyncio.run(serve_http(server, *parse_http(args.http)))
        return 0
    admin = os.environ.get(ADMIN_TOKEN_ENV)
    if not admin or args.http is None:
        parser.error(f"a shared server needs --http and ${ADMIN_TOKEN_ENV}")
    asyncio.run(serve_http(WorldServer(admin_token=admin), *parse_http(args.http)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
