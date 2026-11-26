"""Configuration settings for ACP Bridge."""

from dataclasses import dataclass


@dataclass
class BridgeSettings:
    """Settings for the ACP Bridge server."""

    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    allow_origins: list[str] | None = None
    token: str | None = None
    """Bearer token clients must send; required unless ``host`` is a loopback host."""
    allow_unauthenticated: bool = False
    """Serve without a token off loopback anyway (e.g. behind an authenticating proxy)."""
