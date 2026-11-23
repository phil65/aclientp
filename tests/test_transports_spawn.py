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
