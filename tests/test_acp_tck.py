"""Conformance checks against the official ACP Test Compatibility Kit (``acp-tck``).

Drives :mod:`tests.tck_agent` (built on ``aclientp``'s router, v1/v2 runtimes,
and Streamable HTTP server/client) over both direct stdio and Streamable HTTP
(``aclientp-connect`` -> ``AcpHttpServer``).

Opt-in (requires ``uvx`` and network on first run to fetch ``acp-tck``)::

    ACP_TCK=1 uv run pytest tests/test_acp_tck.py -v
"""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any

import httpx2
import pytest

from aclientp.transports import HttpTransport, serve
from tests.tck_agent import CANCEL_MARKER, V1TckAgent, V2TckAgent


RUN_TCK = bool(os.environ.get("ACP_TCK"))
TCK_REF = os.environ.get("ACP_TCK_REF", "v0.5.0")
AGENT_SCRIPT = Path(__file__).with_name("tck_agent.py")

pytestmark = [
    pytest.mark.skipif(not RUN_TCK, reason="Set ACP_TCK=1 to run acp-tck conformance suite"),
    pytest.mark.timeout(600),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _run_tck(
    version: int, agent_cmd: list[str], report_path: Path
) -> tuple[int, dict[str, Any], str]:
    cmd = [
        "uvx",
        "--from",
        f"git+https://github.com/agentclientprotocol/acp-tck@{TCK_REF}",
        "acp-tck",
        "--protocol-version",
        str(version),
        "--timeout",
        "15",
        "--test-timeout",
        "45",
        "--cancel-prompt",
        CANCEL_MARKER,
        "--report-json",
        str(report_path),
        "--",
        *agent_cmd,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    output = completed.stdout + "\n" + completed.stderr
    if not report_path.exists():
        pytest.fail(f"acp-tck wrote no report (exit {completed.returncode}):\n{output[-4000:]}")
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    return completed.returncode, report, output


def _format_failures(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for req in report["requirements"]:
        if req["status"] == "FAIL" or (
            req["tier"] == "MANDATORY" and req["status"] == "NOT_TESTED"
        ):
            lines.append(f"[{req['tier']}] {req['id']} ({req['status']})")
            for test in req.get("tests", []):
                if test["status"] == "FAIL":
                    msg = (test.get("message") or "").splitlines()
                    detail = next(
                        (line for line in msg if line.startswith("E ")),
                        msg[-1] if msg else "",
                    )
                    lines.append(f"  - {test['nodeid']}: {detail.strip()}")
    return "\n".join(lines)


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
async def test_acp_tck_stdio(version: int, tmp_path: Path) -> None:
    report_path = tmp_path / f"tck-stdio-v{version}.json"
    code, report, _output = await asyncio.to_thread(
        _run_tck, version, [sys.executable, str(AGENT_SCRIPT)], report_path
    )
    assert not report["verdict"]["blocked_by_auth"]
    assert not report["verdict"]["blocked_by_version_mismatch"]
    assert report["verdict"]["conformant"], (
        f"acp-tck v{version} (stdio) non-conformant (exit {code}):\n{_format_failures(report)}"
    )
    assert code == 0


@pytest.mark.parametrize("version", [1, 2], ids=["v1", "v2"])
async def test_acp_tck_streamable_http(version: int, tmp_path: Path) -> None:
    port = _free_port()
    shutdown = asyncio.Event()
    transport = HttpTransport(host="127.0.0.1", port=port)
    server_task = asyncio.create_task(
        serve(
            lambda conn: V1TckAgent(conn),
            transport,
            v2_agent=lambda conn: V2TckAgent(conn),
            shutdown_event=shutdown,
        )
    )
    endpoint = f"http://127.0.0.1:{port}/acp"
    try:
        async with httpx2.AsyncClient() as http:
            for _ in range(100):
                try:
                    await http.get(endpoint)
                    break
                except httpx2.ConnectError:
                    await asyncio.sleep(0.05)

        report_path = tmp_path / f"tck-http-v{version}.json"
        agent_cmd = [sys.executable, "-m", "aclientp.http_relay", endpoint]
        code, report, _output = await asyncio.to_thread(_run_tck, version, agent_cmd, report_path)
        assert not report["verdict"]["blocked_by_auth"]
        assert not report["verdict"]["blocked_by_version_mismatch"]
        assert report["verdict"]["conformant"], (
            f"acp-tck v{version} (http) non-conformant (exit {code}):\n{_format_failures(report)}"
        )
        assert code == 0
    finally:
        shutdown.set()
        await asyncio.wait_for(server_task, 10)
