"""Running agents with `run_agent`."""

from __future__ import annotations

import asyncio
import os
import sys

import anyio
import pytest

from aclientp.agent.implementations.testing import TestAgent as EchoAgent
from aclientp.stdio import StdinStream, run_agent
from tests.streams import TIMEOUT, make_streams


async def test_run_agent_returns_when_client_disconnects() -> None:
    writer, reader, peer = make_streams()
    runner = asyncio.create_task(run_agent(EchoAgent(), writer, reader))
    await peer.send({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": 1},
    })
    assert (await peer.receive())["result"]["protocolVersion"] == 1
    await peer.hang_up()
    await asyncio.wait_for(runner, TIMEOUT)


async def test_run_agent_requires_both_or_nomake_streams() -> None:
    writer, _reader, _peer = make_streams()
    with pytest.raises(ValueError, match="both"):
        await run_agent(EchoAgent(), writer, None)


async def test_v1_test_agent_negotiates_down_to_v1() -> None:
    writer, reader, peer = make_streams()
    runner = asyncio.create_task(run_agent(EchoAgent(), writer, reader))
    await peer.send({
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {"protocolVersion": 2},
    })
    assert (await peer.receive())["result"]["protocolVersion"] == 1
    await peer.hang_up()
    await asyncio.wait_for(runner, TIMEOUT)


# --- stdin reader -----------------------------------------------------------------

AGENT_EXITS_ON_ITS_OWN = """
import asyncio
from aclientp.agent.implementations.testing import TestAgent
from aclientp.stdio import run_agent

async def main():
    try:
        await asyncio.wait_for(run_agent(TestAgent()), 0.3)
    except TimeoutError:
        pass

asyncio.run(main())
"""


async def test_agent_exits_while_the_client_keeps_stdin_open() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        AGENT_EXITS_ON_ITS_OWN,
        stdin=asyncio.subprocess.PIPE,  # Kept open, never written to
        stdout=asyncio.subprocess.DEVNULL,
    )
    try:
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_stdin_stream_reads_and_keeps_data_of_a_cancelled_receive() -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        stream = StdinStream(reader.fileno())
        pending = asyncio.create_task(stream.receive())
        await asyncio.sleep(0.05)
        pending.cancel()
        writer.write(b"kept\n")
        await asyncio.sleep(0.05)  # The thread delivers to the cancelled receive
        assert await asyncio.wait_for(stream.receive(), TIMEOUT) == b"kept\n"
        writer.write(b"more")
        assert await asyncio.wait_for(stream.receive(), TIMEOUT) == b"more"
        writer.close()
        with pytest.raises(anyio.EndOfStream):
            await asyncio.wait_for(stream.receive(), TIMEOUT)
