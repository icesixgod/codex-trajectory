#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zstandard==0.25.0; python_version < '3.14'"]
# ///
"""Start the opted-in trajectory watcher early in the Codex session lifecycle."""

from codex_trajectory.cdp_peer import discover_host_identity
from codex_trajectory.cdp_settings import reconcile_daemon


def main() -> int:
    """Reconcile only when this hook is a descendant of an authenticated Codex host."""
    if discover_host_identity() is None:
        return 0
    try:
        reconcile_daemon()
    except OSError:
        # MCP startup and the viewer recovery path can safely retry later.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
