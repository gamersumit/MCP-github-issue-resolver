"""Tool-handler subpackage for the MCP server.

Each module implements one cluster of MCP tools as async functions
that take a :class:`ghia.app.GhiaApp` and return a
:class:`ghia.errors.ToolResponse`.  The FastMCP entrypoint in
``server.py`` adapts them to MCP-tool signatures.
"""
