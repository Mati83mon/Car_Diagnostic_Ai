# Majster-AI — Automotive UDS Diagnostic Agent via MCP

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Protocol](https://img.shields.io/badge/Protocol-UDS%20%7C%20CAN-orange)
![AI](https://img.shields.io/badge/AI-LangGraph%20%7C%20Claude-purple)
![MCP](https://img.shields.io/badge/Architecture-MCP-brightgreen)
![Safety](https://img.shields.io/badge/Default-READ__ONLY-red)
![License](https://img.shields.io/badge/License-MIT-green)

**Car_Diagnostic_AI** — also answering to **CDA** and **Majster-AI** — is a
diagnostic agent that connects a large language model to a car's control
modules over UDS/CAN, to a local index of workshop manuals, and to live web
search. The three capabilities are exposed as independent **Model Context
Protocol** servers and orchestrated by a **LangGraph** agent with a hard
human-in-the-loop gate on anything that writes to the vehicle.

Developed and tested against a **Land Rover Freelander 2 (2010, 2.2 TD4)**.

> ### ⚠️ It defaults to a simulator, and to READ_ONLY
>
> A fresh clone cannot transmit on a real bus and cannot write to a vehicle.
> Both take a deliberate configuration change. **Read [docs/SAFETY.md](docs/SAFETY.md)
> before connecting to a car you care about.**

---

## What it actually does

```
you> The car goes into limp mode on the motorway. What's wrong with it?

  [agent.plan] Calling: read_dtc
  [car.read_dtc] Reading DTCs from ECM
  [agent.plan] Calling: read_live_data
  [agent.plan] Calling: search_manual

majster> Three codes stored in the ECM, and I think two of them are one fault:

  P0299 (confirmed) — turbocharger underboost
  P2015 (confirmed) — intake manifold runner position sensor
  P0401 (pending)   — EGR flow insufficient

  Live data backs up the underboost: manifold pressure reads 101 kPa at idle
  against 100 kPa barometric, so the turbo isn't building any boost at all.

  P0299 and P2015 together on a DW12 is a familiar pattern — both are air-path
  faults and the swirl-flap linkage sits right next to the actuator. The
  manual (fl2_engine.pdf, page 412) says actuator vacuum should reach 0.6 bar
  at idle; that's the measurement that would separate a seized actuator rod
  from a boost leak, and it's a five-minute check before anyone buys a turbo.

  The pending EGR code I'd leave alone for now — it hasn't confirmed, and a
  fouled EGR is a plausible consequence of running underboost rather than a
  separate fault.
```

Note what it did not do: guess, and offer to clear the codes.

---

## Architecture

```
                     +---------------------------+
                     |     LangGraph agent       |
                     |  Claude Opus 5 / Ollama   |
                     +-------------+-------------+
                                   |
                 +-----------------+-----------------+
                 |                 |                 |
       +---------v------+ +--------v-------+ +-------v--------+
       | Car_Interface  | | RAG_Workshop   | |  Web_Search    |
       |      MCP       | |      MCP       | |      MCP       |
       +---------+------+ +--------+-------+ +-------+--------+
                 |                 |                 |
        UDS over CAN       local manuals      Tavily / DuckDuckGo
```

* **Car_Interface_MCP** — `python-can` + `udsoncan`. Reads DTCs, live data and
  raw DIDs from any module; clears codes only through the approval handshake.
  Five interchangeable backends plus a built-in ECU simulator.
* **RAG_Workshop_MCP** — ChromaDB over your own workshop manual PDFs. Runs
  entirely on-device; every answer carries a file-and-page citation.
* **Web_Search_MCP** — Tavily with a keyless DuckDuckGo fallback, weighted
  towards Land Rover forums.
* **The orchestrator** — READ_ONLY by default; every write pauses the graph
  and waits for a human.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Install

```bash
git clone https://github.com/Mati83mon/Car_Diagnostic_Ai.git
cd Car_Diagnostic_Ai

python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate

pip install -e ".[all,dev]"         # or: pip install -r requirements.txt

cp .env.example .env                # then edit it
```

On Termux or another ARM device where the heavy extras will not build, skip
them — the project is designed to work without:

```bash
pip install -e ".[car,mcp,agent,web]"
```

### Check it works

```bash
majster-ai doctor
```

Also available as `cda` and `car-diagnostic-ai`, or `python main.py <command>`.

---

## Configure

Everything lives in `.env`. The defaults are safe, so an empty file is a valid
file. The settings that matter most:

```bash
# --- safety -----------------------------------------------------------
MAJSTER_WRITE_ENABLED=false      # master switch. Leave false.
MAJSTER_REQUIRE_APPROVAL=true    # human-in-the-loop. Leave true.

# --- vehicle interface -------------------------------------------------
MAJSTER_CAN_BACKEND=virtual      # virtual|socketcan|slcan|serial|j2534|rfcomm
MAJSTER_CAN_CHANNEL=can0
MAJSTER_CAN_BITRATE=500000

# --- LLM ----------------------------------------------------------------
ANTHROPIC_API_KEY=sk-ant-...     # Claude Opus 5; falls back to Ollama if unset
MAJSTER_OLLAMA_MODEL=qwen2.5:7b-instruct

# --- web search ---------------------------------------------------------
TAVILY_API_KEY=tvly-...          # optional; DuckDuckGo is used without it
```

See [`.env.example`](.env.example) for every option, annotated.

---

## Use

### Interactive

```bash
majster-ai chat
```

### One-shot

```bash
majster-ai ask "why is the DPF light on?"
```

### Direct tools, no LLM

```bash
majster-ai dtc --module ECM              # read fault codes
majster-ai dtc --all                     # scan every module
majster-ai live RPM COOLANT_TEMP MAF     # read live data
majster-ai scan                          # discover which ECUs answer
majster-ai clear --module ECM            # write: prompts for approval
```

### Workshop manuals

```bash
cp ~/manuals/*.pdf data/manuals/
majster-ai ingest
majster-ai search "swirl flap removal procedure"
```

Manuals are indexed and searched locally. Nothing is uploaded.

### As MCP servers for another client

```bash
majster-ai serve car_interface           # stdio transport
```

Configuration for Claude Desktop and friends is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#using-the-servers-from-another-mcp-client).

---

## Safety model

Four independent layers stand between the model and the vehicle. A write has
to get through all of them.

| Layer | Guarantee |
|---|---|
| **1. Master switch** | `MAJSTER_WRITE_ENABLED=false` by default. Writes are refused outright, no token issued, no prompt shown. The agent cannot change this. |
| **2. Token handshake** | The first call always fails and returns an impact summary plus a single-use token, bound to a hash of the exact arguments and expiring in five minutes. Approval for the ECM cannot clear the ABS module. |
| **3. Human pause** | The graph suspends via `interrupt()`. A refusal, an empty answer, a closed stdin, a crashed UI, a non-interactive session — all mean *no*. |
| **4. System prompt** | Tells the model what the other three layers will do. Treated as the weakest layer, because it is. |

Layer 2 lives in the service behind the MCP server, so it protects the car
even when something other than this agent is driving.

```
========================================================================
  WRITE OPERATION - HUMAN APPROVAL REQUIRED
========================================================================
  Operation : clear_dtc
  Module    : ECM (Engine Control Module - 2.2 TD4)
  Scope     : ALL stored DTCs in this module
  Risk      : MEDIUM        Reversible: NO
  Will erase 3 code(s):
      - P0299-00     Turbocharger/Supercharger A Underboost Condition
      - P2015-00     Intake Manifold Runner Position Sensor/Switch Circuit
      - P0401-00     Exhaust Gas Recirculation Flow Insufficient Detected

  Consequences:
      ! Freeze-frame data captured when the fault occurred will be lost.
      ! Readiness monitors reset; the vehicle may fail an emissions test.
      ! Clearing does not repair anything. If the fault is still present
        the code will return.
========================================================================

  Type 'yes' to authorise, anything else to decline:
```

Full detail in [docs/SAFETY.md](docs/SAFETY.md).

---

## Hardware

| Backend | Interface | Platform |
|---|---|---|
| `virtual` | none — built-in simulator | anywhere |
| `j2534` | Tactrix Openport 2.0 | Linux, Windows |
| `socketcan` | USB2CAN, CANable, PiCAN | Linux |
| `slcan` | CANable/CANtact (slcan firmware) | Linux, macOS, Windows |
| `rfcomm` | ELM327 / OBDLink over Bluetooth | Linux, **Termux** |

The Freelander 2 uses ISO 15765-4 CAN at 500 kbit/s with 11-bit identifiers,
on OBD-II pins 6 (CAN-H) and 14 (CAN-L).

Setup for each, plus Termux, Raspberry Pi, and which ELM327 adapters actually
work: [docs/HARDWARE.md](docs/HARDWARE.md).

---

## About the data in this repository

Only two diagnostic addresses on any car are legislated and therefore certain:
the powertrain addresses `0x7E0`/`0x7E8` and `0x7E1`/`0x7E9`. Everything else
in the built-in module map is community-derived and ships marked
`verified: false`.

The same applies to live-data scaling: the SAE J1979 PIDs are standard and
marked verified; manufacturer DIDs are proprietary and **none ship at all**,
because a confidently-wrong number is the worst possible output from a
diagnostic tool.

To find out what is true for *your* car:

```bash
majster-ai scan
```

Then record what answered in `data/modules.json`. See
[docs/FREELANDER2.md](docs/FREELANDER2.md).

---

## Development

```bash
make install-dev
make check                # black --check, flake8, pytest
make test-cov             # coverage report
```

The entire suite runs against the in-process ECU simulator — no hardware, no
API key, no network:

```
694 passed, 1 skipped
```

The simulator is a real UDS implementation rather than a mock, so the retry
logic, the DTC codec, the MCP tools and the HITL gate all run against the same
byte stream they will see on a real bus. Fault injection makes the flaky-bus
paths deterministic:

```python
ecm.inject_faults(drop_next=2)      # two silent timeouts, then fine
ecm.inject_faults(pending_next=3)   # three NRC 0x78, then the answer
ecm.inject_faults(busy_next=1)      # one NRC 0x21 busyRepeatRequest
```

CI runs lint, the suite on Python 3.10/3.11/3.12, the safety invariants as
their own job, and an integration job that spawns the MCP servers as real
subprocesses.

---

## Project layout

```
majster_ai/
├── agent/              LangGraph orchestrator, HITL, LLM providers
├── mcp_servers/
│   ├── car_interface/  UDS/CAN — the safety gate lives in service.py
│   ├── rag_workshop/   local manual retrieval
│   └── web_search/     Tavily / DuckDuckGo
├── config.py           settings and the two safety gates
└── cli.py
tests/                  694 tests, all hardware-free
docs/                   ARCHITECTURE, SAFETY, HARDWARE, FREELANDER2
```

---

## Disclaimer

**This project is for educational and research purposes.** It is not a
certified diagnostic tool and is not a substitute for the manufacturer's
equipment or a qualified technician.

The authors accept no liability for damage to a vehicle, damage to control
modules, failed repairs, voided warranties, or injury arising from use of this
software. You are responsible for understanding what any command does before
it reaches your car's bus. If you are not certain, do not send it.

### Zrzeczenie się odpowiedzialności

**Projekt służy wyłącznie do celów edukacyjnych i badawczych.** Autor nie
ponosi odpowiedzialności za jakiekolwiek uszkodzenia pojazdu, uszkodzenia
sterowników (ECU) lub obrażenia ciała wynikające z użytkowania tego
oprogramowania.

Zawsze upewnij się, że wiesz, jakie komendy — zwłaszcza polecenia **ZAPISU** —
są wysyłane na magistralę CAN Twojego samochodu.

---

## Licence

MIT — see [LICENSE](LICENSE).

Workshop manuals are copyrighted and are not distributed with this project.
`data/manuals/` is gitignored; source your own legally.
