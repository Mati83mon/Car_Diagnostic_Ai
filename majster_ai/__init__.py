"""Majster-AI / Car_Diagnostic_AI (CDA).

An automotive diagnostic AI agent that bridges Large Language Models with
physical vehicle modules over UDS/CAN, a local RAG index of workshop manuals,
and live web search -- all exposed through the Model Context Protocol (MCP).

Target vehicle for development and testing:
    Land Rover Freelander 2 (2010, 2.2 TD4 / DW12 engine family)

The package is import-safe: nothing here opens a CAN interface, spawns a
subprocess, or reaches the network at import time.

Naming
------
The project answers to three interchangeable names, all referring to this
same package::

    Car_Diagnostic_AI   -- the repository / product name
    CDA                 -- the short form
    Majster-AI          -- the agent's own name (``majster`` = PL. "craftsman")
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Canonical name and its accepted aliases. Kept in one place so the CLI,
#: the MCP server banners and the docs cannot drift apart.
PROJECT_NAME = "Car_Diagnostic_AI"
PROJECT_ALIASES = ("Car_Diagnostic_AI", "CDA", "Majster-AI", "majster-ai", "majster_ai")
AGENT_NAME = "Majster-AI"

#: The vehicle this project is developed against. Everything still works on
#: other UDS-capable vehicles, but the module map and DID overlays are FL2.
TARGET_VEHICLE = "Land Rover Freelander 2 (2010, 2.2 TD4)"

__all__ = [
    "__version__",
    "PROJECT_NAME",
    "PROJECT_ALIASES",
    "AGENT_NAME",
    "TARGET_VEHICLE",
]
