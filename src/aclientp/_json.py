"""JSON helpers for peer-controlled input."""

from typing import Any

import anyenv


def decode_object(data: str | bytes) -> dict[str, Any] | None:
    """Decode a JSON object, or return None for anything else.

    Covers malformed JSON, invalid UTF-8 and valid JSON that is not an object
    (``1``, ``[1]``, ``"x"``), so a peer cannot crash a transport with any of them.
    """
    try:
        message = anyenv.load_json(data)
    except anyenv.JsonLoadError, TypeError, UnicodeDecodeError:
        return None
    return message if isinstance(message, dict) else None


def single_line(frame: str) -> str:
    """A JSON text as one NDJSON line.

    Raw CR/LF can only be whitespace between JSON tokens (inside strings they must be
    escaped), so replacing them with spaces keeps the JSON intact.
    """
    return frame.replace("\r", " ").replace("\n", " ")
