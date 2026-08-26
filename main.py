#!/usr/bin/env python3
"""Majster-AI / Car_Diagnostic_AI (CDA) -- entry point.

    python main.py doctor
    python main.py chat
    python main.py dtc --module ECM
    python main.py serve car_interface

Equivalent to the installed ``majster-ai`` / ``cda`` / ``car-diagnostic-ai``
console scripts.

SAFETY: this software can transmit on a vehicle's CAN bus. It defaults to a
built-in simulator and to READ_ONLY. Read docs/SAFETY.md before pointing it at
a car you care about.
"""

from __future__ import annotations

import sys

from majster_ai.cli import main

if __name__ == "__main__":
    sys.exit(main())
