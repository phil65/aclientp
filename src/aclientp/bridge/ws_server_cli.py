"""CLI entry point for the ACP WebSocket server bridge.

Usage:
    python -m aclientp.bridge.ws_server_cli "uv run agentpool serve-acp"
    python -m aclientp.bridge.ws_server_cli --port 8765 -- uv run agentpool serve-acp
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys

import anyio


def main() -> None:
    """Run the ACP WebSocket server bridge from command line."""
    parser = argparse.ArgumentParser(
        description="Bridge a stdio ACP agent to WebSocket transport.",
        epilog=(
            "Examples:\n"
            '  acp-ws-server "uv run agentpool serve-acp"\n'
            "  acp-ws-server --port 8765 -- uv run agentpool serve-acp\n"
            "  acp-ws-server -H 0.0.0.0 -p 9000 -- uv run my-agent\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("command", help="Command to spawn the ACP agent.")
    parser.add_argument("args", nargs="*", help="Arguments for the agent command.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8765,
        help="Port for the WebSocket server. Default: 8765",
    )
    parser.add_argument(
        "-H",
        "--host",
        default="localhost",
        help="Host to bind the server to. Default: localhost",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level. Default: INFO",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("ACP_BRIDGE_TOKEN"),
        help=(
            "Bearer token clients must send (Authorization: Bearer <token>). Required "
            "when binding to a non-loopback host. Default: $ACP_BRIDGE_TOKEN"
        ),
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Browser origin allowed to connect. Can be specified multiple times.",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for the agent subprocess.",
    )

    parsed = parser.parse_args()

    if not parsed.command:
        parser.print_help()
        sys.exit(1)

    from aclientp.server_auth import InsecureServerError, check_exposure

    try:
        check_exposure(parsed.host, authenticated=bool(parsed.token), what="the ACP bridge")
    except InsecureServerError as exc:
        parser.error(f"{exc}; pass --token (or ACP_BRIDGE_TOKEN)")

    logging.basicConfig(
        level=getattr(logging, parsed.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Reduce websockets noise
    logging.getLogger("websockets").setLevel(logging.WARNING)

    from aclientp.bridge.ws_server import ACPWebSocketServer
    from aclientp.server_auth import bearer_token_auth

    server = ACPWebSocketServer(
        command=parsed.command,
        args=parsed.args,
        host=parsed.host,
        port=parsed.port,
        cwd=parsed.cwd,
        authenticate=bearer_token_auth(parsed.token) if parsed.token else None,
        allow_origins=parsed.allow_origin,
    )

    print(f"🔗 ACP WebSocket bridge starting on ws://{parsed.host}:{parsed.port}")
    print(f"   Agent command: {parsed.command} {' '.join(parsed.args)}")

    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(server.serve)


if __name__ == "__main__":
    main()
