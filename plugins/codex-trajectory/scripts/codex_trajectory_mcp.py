#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Start the dependency-free Codex Trajectory MCP server."""

from contextlib import suppress

from codex_trajectory.cdp_settings import reconcile_daemon
from codex_trajectory.protocol import main

if __name__ == "__main__":
    # The MCP server and read-only trajectory tools remain usable even when the
    # optional, previously enabled local watcher cannot be reconciled.
    with suppress(OSError):
        reconcile_daemon()
    main()
