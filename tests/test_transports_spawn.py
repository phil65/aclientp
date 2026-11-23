"""Spawning stdio agents with `spawn_stdio_transport`."""

from __future__ import annotations

import sys

from aclientp.transports import spawn_stdio_transport


SCRIPT = """
import sys
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
