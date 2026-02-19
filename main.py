from __future__ import annotations

import os

from mcstatus_mcp.server import mcp


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError("MCP_TRANSPORT must be one of: stdio, sse, streamable-http.")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
