"""The stall handler's removal task is kept referenced and awaited on close."""

import asyncio

from aclientp.http_server import AcpHttpServer


async def _serve(reader: object, writer: object) -> None:
    await asyncio.sleep(0)


async def test_stall_removal_task_is_tracked_and_finished() -> None:
    server = AcpHttpServer(_serve)
    connection, _receive = server._open("owner")
    output = server._output(connection, lambda _message: None)
    output._on_stall()
    assert len(server._background) == 1
    await server.close()
    assert not server._background
    assert connection.connection_id not in server.connections
