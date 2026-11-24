"""The sidecar's egress proxy: HTTPS to the model endpoints a harness calls itself, and nothing else.

A docker box sits on an internal network whose only way out is the sidecar. A harness that talks to its model
provider directly (not through the meter) gets `HTTPS_PROXY` pointing here. A CONNECT to an allowed host on port 443
is tunnelled; every other request is refused, so the box keeps no general internet access.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import sys

# Model endpoints only: a host listed here, or any subdomain of it.
ALLOWED_HOSTS = ("openrouter.ai",)
ALLOWED_PORT = 443
HEADER_LIMIT = 16 * 1024
CONNECT_TIMEOUT_S = 15.0


def allowed(host: str, port: int, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS) -> bool:
    host = host.lower().rstrip(".")
    return port == ALLOWED_PORT and any(host == h or host.endswith("." + h) for h in allowed_hosts)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def handle(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
) -> None:
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
        method, target, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        host, _, port_text = target.rpartition(":")
        if method != "CONNECT" or not port_text.isdigit() or not allowed(host, int(port_text), allowed_hosts):
            client_w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await client_w.drain()
            client_w.close()
            return
        upstream_r, upstream_w = await asyncio.wait_for(
            asyncio.open_connection(host, int(port_text)), CONNECT_TIMEOUT_S
        )
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        with contextlib.suppress(Exception):
            client_w.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await client_w.drain()
        client_w.close()
        return
    client_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await client_w.drain()
    await asyncio.gather(pipe(client_r, upstream_w), pipe(upstream_r, client_w))


async def serve(host: str, port: int, allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS) -> None:
    handler = functools.partial(handle, allowed_hosts=allowed_hosts)
    server = await asyncio.start_server(handler, host, port, limit=HEADER_LIMIT)
    async with server:
        await server.serve_forever()


def main() -> None:
    host, _, port = sys.argv[1].rpartition(":")
    configured = tuple(host for host in os.environ.get("PARALLAX_EGRESS_HOSTS", "").split(",") if host) or ALLOWED_HOSTS
    asyncio.run(serve(host, int(port), configured))


if __name__ == "__main__":
    main()
