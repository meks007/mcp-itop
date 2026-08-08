# low_level_resource_types.py
# Shared type alias for low-level MCP resource handlers.
# Import this in every tool module that provides get_low_level_resource_handlers()
# and in server.py where the central router is installed.

from collections.abc import Awaitable, Callable

import mcp.types as types

# A low-level resource handler returns a fully-formed ServerResult directly,
# bypassing FastMCP's high-level serialisation layer.
LowLevelResourceHandler = Callable[[], Awaitable[types.ServerResult]]
