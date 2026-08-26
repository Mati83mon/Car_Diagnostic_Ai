"""The three MCP servers that give Majster-AI its senses.

* :mod:`majster_ai.mcp_servers.car_interface` -- UDS/CAN access to the vehicle.
* :mod:`majster_ai.mcp_servers.rag_workshop`  -- local workshop-manual retrieval.
* :mod:`majster_ai.mcp_servers.web_search`    -- live web / forum search.

Each server is an independent process speaking MCP over stdio. They are also
importable as plain Python, which is how the test-suite exercises them without
a transport in the way.
"""

from __future__ import annotations

__all__ = ["car_interface", "rag_workshop", "web_search"]
