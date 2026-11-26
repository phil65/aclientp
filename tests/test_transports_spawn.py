"""Spawning stdio agents with `spawn_stdio_transport`."""

from __future__ import annotations

import sys

import anyio

from aclientp.transports import spawn_stdio_transport


SCRIPT = """
import sys

import anyio
sys.stderr.write("first line\\nsecond ")
sys.stderr.flush()
sys.stderr.write("line\\ncrashed without newline")
sys.stderr.flush()
sys.stdin.read()
"""


async def test_on_stderr_gets_every_line_including_the_last() -> None:
    lines: list[str] = []
    async with spawn_stdio_transport(sys.executable, "-c", SCRIPT, on_stderr=lines.append) as (
        _reader,
        _writer,
        process,
    ):
        pass

    assert process.returncode == 0
    assert lines == ["first line", "second line", "crashed without newline"]


async def test_unread_stderr_is_drained_so_the_agent_never_blocks() -> None:
    # 1 MiB on stderr: far more than a pipe buffer. Without draining, the child would
    # block on write and never read stdin / exit.
    script = "import sys; sys.stderr.write('x' * (1 << 20)); sys.stderr.flush(); sys.stdin.read()"
    with anyio.fail_after(10):
        async with spawn_stdio_transport(sys.executable, "-c", script) as (_r, _w, process):
            await anyio.sleep(0.5)
    assert process.returncode == 0


async def test_child_is_stopped_when_the_caller_is_cancelled() -> None:
    script = "import time; time.sleep(30)"
    with anyio.move_on_after(0.5):
        async with spawn_stdio_transport(sys.executable, "-c", script, shutdown_timeout=0.5) as (
            _r,
            _w,
            process,
        ):
            await anyio.sleep(30)
    assert process.returncode is not None


async def test_spawned_agent_is_stopped_when_the_caller_is_cancelled() -> None:
    from aclientp.client.implementations.noop_client import NoOpClient
    from aclientp.stdio import spawn_agent_process

    script = "import time; time.sleep(30)"
    spawned = spawn_agent_process(lambda _agent: NoOpClient(), sys.executable, "-c", script)
    with anyio.move_on_after(0.5):
        async with spawned as (_conn, process):
            await anyio.sleep(30)
    assert process.returncode is not None
