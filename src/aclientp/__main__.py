"""ACP package main entry point.

Allows running the ACP debug server with:
    python -m aclientp
"""

if __name__ == "__main__":
    from aclientp.agent.implementations.debug_server.cli import main

    main()
