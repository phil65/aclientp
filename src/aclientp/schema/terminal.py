"""Terminal schema definitions."""

from aclientp.schema.base import AnnotatedObject
from aclientp.schema.lenient import DefaultOnError  # noqa: TC001


class TerminalExitStatus(AnnotatedObject):
    """Exit status of a terminal command."""

    exit_code: DefaultOnError[int | None] = None
    """The process exit code (may be null if terminated by signal)."""

    signal: DefaultOnError[str | None] = None
    """The signal that terminated the process (may be null if exited normally)."""
