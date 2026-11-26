"""Writing and reading through `ACPFileSystem.open()` (fsspec's sync file API)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from aclientp.client.implementations import HeadlessACPClient
from aclientp.filesystem import ACPFileSystem


if TYPE_CHECKING:
    from pathlib import Path


def test_open_write_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.txt"
    # A local file at the remote path's name must not be what gets uploaded.
    monkeypatch.chdir(tmp_path)
    client = HeadlessACPClient(
        working_dir=tmp_path, allow_file_operations=True, auto_grant_permissions=True
    )
    fs = ACPFileSystem(client=client, session_id="s")
    with fs.open(str(target), "wb") as f:
        f.write(b"hello")
    assert target.read_text() == "hello"
    with fs.open(str(target), "rb") as f:
        assert f.read() == b"hello"


async def test_truncated_binary_read_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aclientp import filesystem

    target = tmp_path / "image.png"
    target.write_bytes(bytes(range(256)) * 4)
    monkeypatch.setattr(filesystem, "MAX_COMMAND_OUTPUT_BYTES", 16)
    client = HeadlessACPClient(
        working_dir=tmp_path, allow_file_operations=True, auto_grant_permissions=True
    )
    fs = ACPFileSystem(client=client, session_id="s")
    try:
        with pytest.raises(FileNotFoundError, match="truncated"):
            await fs._cat_file(str(target))
    finally:
        await client.cleanup()


async def test_complete_binary_read(tmp_path: Path) -> None:
    target = tmp_path / "image.png"
    data = bytes(range(256)) * 4
    target.write_bytes(data)
    client = HeadlessACPClient(
        working_dir=tmp_path, allow_file_operations=True, auto_grant_permissions=True
    )
    fs = ACPFileSystem(client=client, session_id="s")
    try:
        assert await fs._cat_file(str(target)) == data
    finally:
        await client.cleanup()
