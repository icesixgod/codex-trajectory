#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zstandard==0.25.0; python_version < '3.14'"]
# ///
"""Start the Codex Trajectory MCP server."""

import os
import threading
import time
from contextlib import suppress

from codex_trajectory.cdp_settings import MCP_STARTED_AT_ENV, reconcile_daemon
from codex_trajectory.projection import prewarm_caches
from codex_trajectory.protocol import main as protocol_main


def _initialize_in_background() -> None:
    """Restore the watcher and warm read-only caches without delaying MCP."""
    with suppress(OSError):
        reconcile_daemon()
    with suppress(OSError, RuntimeError, ValueError):
        prewarm_caches()


def _start_background_initialization() -> threading.Thread:
    """Start one bounded daemon worker for optional cold-start work."""
    worker = threading.Thread(
        target=_initialize_in_background,
        name="codex-trajectory-initialize",
        daemon=True,
    )
    worker.start()
    return worker


def main() -> None:
    """Serve MCP immediately while optional cold-start work runs in parallel."""
    os.environ[MCP_STARTED_AT_ENV] = str(time.time())
    _start_background_initialization()
    protocol_main()


if __name__ == "__main__":
    main()
