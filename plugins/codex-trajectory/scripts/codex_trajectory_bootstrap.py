#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zstandard==0.25.0; python_version < '3.14'"]
# ///
"""Start the opted-in trajectory watcher early in the Codex session lifecycle."""

import os

from codex_trajectory.cdp_peer import discover_host_identity
from codex_trajectory.cdp_settings import read_settings, reconcile_daemon
from codex_trajectory_cdp import watch

IS_WINDOWS = os.name == "nt"


def main() -> int:
    """Restore the opted-in watcher below an authenticated Codex desktop host."""
    host_identity = discover_host_identity()
    if host_identity is None:
        return 0
    try:
        if IS_WINDOWS:
            # The async hook is already tied to the Codex session. Keeping the
            # watcher in this windowless process avoids relying on a child process
            # escaping the host's Windows Job Object.
            if read_settings()["enabled"] is True:
                return watch(host_identity)
        else:
            reconcile_daemon()
    except OSError:
        # MCP startup and the viewer recovery path can safely retry later.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
