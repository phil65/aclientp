"""Optional tracing: spans via logfire when it is installed, no-ops otherwise."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable


def instrument[**P, R](message: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Trace calls of the decorated function with ``logfire.instrument`` if available.

    Install the ``tracing`` extra (``aclientp[tracing]``) to get spans.
    """
    if importlib.util.find_spec("logfire") is None:

        def passthrough(func: Callable[P, R]) -> Callable[P, R]:
            return func

        return passthrough
    import logfire

    return logfire.instrument(message)
