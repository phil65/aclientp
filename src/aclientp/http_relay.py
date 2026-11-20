"""Relay a stdio ACP client to a remote agent over Streamable HTTP.

ACP clients that can only launch local agents (Zed, JetBrains, ...) spawn this as their
agent; it forwards the newline-delimited JSON-RPC from stdin to the remote ``/acp``
endpoint and the server's messages back to stdout, unchanged.

    aclientp-connect https://example.com/api/acp -H 'X-API-Key: ${MY_API_KEY}'

Header values may reference environment variables (``${NAME}``), so secrets can stay in
the client's agent environment instead of its arguments. stdout carries protocol
messages only; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import string
import sys

import anyio

from aclientp.http_transport import connect_http_transport
from aclientp.stdio import stdio_streams


logger = logging.getLogger(__name__)


def parse_header(raw: str, env: dict[str, str] | None = None) -> tuple[str, str]:
    """Parse ``Name: value``, expanding ``${VAR}`` references in the value.

    Raises:
        ValueError: The header is malformed or references an unset variable.
    """
    name, sep, value = raw.partition(":")
    if not sep or not name.strip():
        msg = f"Invalid header {raw!r}, expected 'Name: value'"
        raise ValueError(msg)
    try:
        expanded = string.Template(value.strip()).substitute(env if env is not None else os.environ)
    except KeyError as exc:
        msg = f"Header {name.strip()!r} references unset environment variable {exc.args[0]}"
        raise ValueError(msg) from exc
    return name.strip(), expanded


async def relay(url: str, headers: dict[str, str]) -> None:
    """Forward stdin to the remote agent and its messages to stdout until either side ends."""
    stdin, stdout = await stdio_streams()
    async with (
        connect_http_transport(url, headers=headers) as (remote_reader, remote_writer),
        anyio.create_task_group() as tg,
    ):

        async def pump(source: anyio.abc.ByteReceiveStream, sink: anyio.abc.ByteSendStream) -> None:
            try:
                async for chunk in source:
                    await sink.send(chunk)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass
            # Either side ending ends the relay (don't linger as an orphan).
            tg.cancel_scope.cancel()

        tg.start_soon(pump, stdin, remote_writer)
        tg.start_soon(pump, remote_reader, stdout)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aclientp-connect",
        description="Relay a stdio ACP client to a remote agent over Streamable HTTP.",
    )
    parser.add_argument("url", help="The agent's /acp endpoint")
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        help="Header for every request, 'Name: value' (${VAR} is expanded); repeatable",
    )
    parser.add_argument("--log-level", default="WARNING", help="Log level (stderr)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)
    try:
        headers = dict(parse_header(raw) for raw in args.header)
    except ValueError as exc:
        parser.error(str(exc))
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(relay, args.url, headers)


if __name__ == "__main__":
    main()
