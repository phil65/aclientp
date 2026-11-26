"""Writing and reading through `ACPFileSystem.open()` (fsspec's sync file API)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aclientp.client.implementations import HeadlessACPClient
from aclientp.filesystem import ACPFileSystem


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
