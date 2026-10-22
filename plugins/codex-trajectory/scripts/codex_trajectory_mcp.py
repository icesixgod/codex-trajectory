#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zstandard==0.25.0; python_version < '3.14'"]
# ///
"""Start the Codex Trajectory MCP server."""

from codex_trajectory.protocol import main as protocol_main


def main() -> None:
    """Serve MCP without reading task history until a tool requests it."""
    protocol_main()


if __name__ == "__main__":
    main()
