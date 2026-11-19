"""MIME helpers for the ACP filesystem: decide between text and binary reads."""

from __future__ import annotations

import mimetypes


# A table that does not read the system's mime.types files: those map common source
# files to non-text types (``.yaml`` -> ``application/yaml``, ``.rs`` ->
# ``application/rls-services+xml``), and their content differs between machines.
_MIME_TYPES = mimetypes.MimeTypes()
_MIME_TYPES.add_type("text/markdown", ".md")
_MIME_TYPES.add_type("text/markdown", ".mdx")
_MIME_TYPES.add_type("image/webp", ".webp")

BINARY_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/octet-stream",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/x-7z-compressed",
    "application/x-bzip2",
    "application/x-xz",
    "application/pdf",
    "application/x-executable",
    "application/x-sharedlib",
    "application/vnd.openxmlformats-officedocument.",
    "application/vnd.oasis.opendocument.",
    "application/msword",
    "application/vnd.ms-",
)
"""MIME type prefixes of content that is definitely binary."""


def guess_type(path: str) -> str | None:
    """Guess the MIME type of a path from its extension; ``None`` if unknown."""
    mime_type, _ = _MIME_TYPES.guess_type(path)
    return mime_type


def is_binary_mime(mime_type: str | None) -> bool:
    """Whether content of this MIME type is definitely binary.

    Unknown types count as text, so source files without a registered type are read
    with ``fs/read_text_file``.
    """
    return mime_type is not None and mime_type.startswith(BINARY_MIME_PREFIXES)
