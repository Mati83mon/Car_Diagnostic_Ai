# Architecture

## Overview

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
          +---------v------+ +--------v-------+ +-------v--------+
          |  UDS over CAN  | | ChromaDB +     | | Tavily /       |
          |  the vehicle   | | local manuals  | | DuckDuckGo     |
          +----------------+ +----------------+ +----------------+
```

Three MCP servers, one orchestrator. Each server is an independent process
speaking JSON-RPC over stdio, and each is usable by any MCP client -- Claude
Desktop, an IDE, your own script -- not only by this agent.

## Why MCP rather than direct function calls

The vehicle interface has to be a process boundary, not a library call:

* **Blast radius.** A crash in the CAN stack takes down one process, not the
  agent mid-diagnosis.
* **The safety gate has to hold for everybody.** The write handshake lives in
  the service behind the MCP server, so it protects the car even when
  something other than our LangGraph agent is driving.
* **Reuse.** Point Claude Desktop at `car_interface` and you have vehicle
  diagnostics in a chat window with no extra code.

## Package layout

```
majster_ai/
├── config.py               settings, secrets, the two safety gates
├── errors.py               typed exception hierarchy -> structured tool errors
├── logging_setup.py        reasoning trail (INFO) + CAN frames (DEBUG)
├── cli.py                  majster-ai / cda / car-diagnostic-ai
├── agent/
│   ├── graph.py            StateGraph; the write handshake and interrupt()
│   ├── runner.py           interrupt/resume loop, console REPL
│   ├── hitl.py             approval requests, decisions, approvers
│   ├── llm.py              Claude Opus 5 primary, Ollama fallback
│   ├── toolkit.py          MCP tool definitions -> LangChain tools
│   ├── prompts.py          system prompt: evidence hierarchy, safety
│   └── state.py            graph state, including the approval audit trail
└── mcp_servers/
    ├── car_interface/
    │   ├── server.py       MCP tool surface (10 tools)
    │   ├── service.py      THE SAFETY GATE - the only path to the bus
    │   ├── uds_client.py   retries, timeouts, NRC 0x78, stale frames
    │   ├── transport.py    UdsTransport: send()/recv() of UDS payloads
    │   ├── backends.py     one config switch, five physical layers
    │   ├── simulator.py    in-process Freelander 2 ECUs
    │   ├── j2534.py        SAE J2534 PassThru via ctypes (Tactrix)
    │   ├── elm327.py       ELM327 over serial / Bluetooth RFCOMM
    │   ├── dtc.py          DTC codec and ISO 14229-1 status bits
    │   ├── pids.py         OBD-II PIDs and UDS DIDs with scaling
    │   └── modules.py      ECU address map, with verified/unverified flags
    ├── rag_workshop/       local manual retrieval, degrades to lexical
    └── web_search/         Tavily -> DuckDuckGo fall-through
```

## The vehicle interface stack

Bottom to top, each layer with one job:

| Layer | Responsibility |
|---|---|
| `backends.TransportFactory` | Pick the physical layer from configuration |
| `transport.UdsTransport` | `send(payload)` / `recv(timeout)` of UDS payloads |
| `uds_client.UdsSession` | Retries, timeouts, NRC 0x78, stale-frame rejection |
| `service.CarInterfaceService` | Decoding, the safety gate, structured results |
| `server` | MCP tool surface with LLM-facing descriptions |

`send`/`recv` are separate rather than one blocking `request` because a single
UDS request may legitimately produce several response frames -- "response
pending" (NRC 0x78) can repeat for seconds on a busy module -- and only the
last one is the real answer.

### Transports

| Implementation | Used by | Notes |
|---|---|---|
| `SimulatedTransport` | `virtual` | In-process ECUs. The default. |
| `IsoTpCanTransport` | `socketcan`, `slcan`, `serial` | python-can + can-isotp |
| `J2534Transport` | `j2534` | ctypes PassThru, ISO15765 protocol |
| `Elm327Transport` | `rfcomm` | AT commands, adapter does ISO-TP |
| `SilentTransport` | `virtual` | A module that is not fitted |

`SilentTransport` matters more than it looks: not every module in the map
exists on every car, and modelling absence as *silence* rather than an
exception is both what a real bus does and what keeps the timeout path
exercised.

## The write handshake

```
   agent                 graph                  service              vehicle
     |                     |                       |                    |
     |-- clear_dtc ------->|                       |                    |
     |                     |-- dry run (no token)->|                    |
     |                     |<-- impact + token ----|   (nothing sent)   |
     |                     |                       |                    |
     |                  interrupt()  ... execution suspended ...        |
     |                     |                                            |
     |                  [ human reads the impact and decides ]          |
     |                     |                                            |
     |                     |-- with token -------->|                    |
     |                     |                       |--- 0x14 ---------->|
     |<-- result ----------|<----------------------|<-------------------|
```

Three properties make this hold:

1. **The dry run has no side effects.** LangGraph re-executes a node from the
   top on resume, so everything before `interrupt()` runs twice. Reading DTCs
   twice is harmless; clearing them twice would not be.
2. **The token is bound to the arguments.** A SHA-256 fingerprint of the
   operation and its arguments is stored with the token, so approval for one
   module cannot be redeemed against another.
3. **The model's own token is discarded.** The graph always strips any
   `confirmation_token` the model supplies and runs its own handshake.
   Honouring one would let a model replay an earlier approval.

## Error handling

Every failure is a subclass of `MajsterError` with a stable `code`, rendered
into a structured payload rather than a traceback:

```json
{
  "ok": false,
  "error": "uds_timeout",
  "message": "ECM did not respond to service 0x19 after 3 attempt(s). The module may be asleep, absent, on a different bus, or the ignition may be off.",
  "module": "ECM",
  "details": {"address": "0x7E0", "attempts": 3}
}
```

Tools never raise into the graph. A model can recover from "that failed, here
is why"; a traceback ends the session.

Partial success is first-class. Asking for twelve live signals when the ECU
implements ten returns ten values and two per-signal failures, because that is
routine on a real car.

## Retry policy

| Condition | Behaviour |
|---|---|
| Timeout | Retry with exponential backoff (`0.25s`, `0.5s`, `1.0s`) |
| NRC 0x78 response pending | Keep waiting on the P2* timeout; **not** a retry |
| NRC 0x21 busy repeat request | Retry after backoff |
| Any other negative response | Definitive -- reported immediately |
| Response for another service | Discarded as stale, keep waiting |

Retrying a definitive refusal ("security access denied") wastes time and can
trip an ECU's anti-scan lockout, so it is never done.

## Degradation

The project is built to run on a phone in a garage, so every optional
dependency has a fallback and each one announces itself:

| Missing | Falls back to | Cost, stated in the tool output |
|---|---|---|
| `sentence-transformers` | Hash embeddings | Lexical matching, not semantic |
| `chromadb` | JSON in-memory store | Linear scan; fine at manual scale |
| `TAVILY_API_KEY` | DuckDuckGo | Lower quality, rate-limited |
| `ANTHROPIC_API_KEY` | Local Ollama | Weaker multi-step tool use |
| Real hardware | Simulator | Synthetic readings, clearly labelled |

A retrieval system that quietly degrades is worse than one that admits it, so
`search_manual` returns a `retrieval_caveat` when it is running on the lexical
fallback, and the agent is told to say so.

## Using the servers from another MCP client

```json
{
  "mcpServers": {
    "car_interface": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "majster_ai.mcp_servers.car_interface.server"],
      "env": {"MAJSTER_CAN_BACKEND": "virtual", "MAJSTER_WRITE_ENABLED": "false"}
    },
    "rag_workshop": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "majster_ai.mcp_servers.rag_workshop.server"]
    },
    "web_search": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "majster_ai.mcp_servers.web_search.server"]
    }
  }
}
```

`majster_ai.agent.toolkit.mcp_server_config()` generates this shape
programmatically.

Note that logging goes to **stderr**: stdout carries JSON-RPC, and a stray
`print()` there corrupts the stream and disconnects the client. There is an
integration test that catches exactly that.

## The web layer

`majster_ai/web/` puts a browser in front of the same services the CLI uses.

```
majster_ai/web/
├── protocol.py    the /ws/diagnostics message contract (mirrored in TS)
├── approvals.py   WebSocketApprover -- the browser as an Approver
├── session.py     one connection: telemetry loop + agent turns
└── app.py         FastAPI: REST, WebSocket, static frontend
```

### The browser is just another Approver

The whole integration is one class. `WebSocketApprover` implements the same
`Approver` interface as `ConsoleApprover`, so the agent, the graph and the
service are untouched — the browser simply becomes another way of answering
the question the console asks.

What it deliberately cannot do is *ask* one. The service's confirmation token
is created and redeemed inside the server process and never appears in any
frame; the client receives an opaque `approval_id` and can send back one
boolean. So the worst a malicious or buggy client can do is answer a question
the operator was already being asked.

Everything ambiguous is a refusal: an id that is not the outstanding one, no
answer inside the window, a socket that drops mid-decision, a second answer to
an already-answered request.

### Two concerns, one bus

A telemetry poller and the agent both reach the same CAN interface. A UDS
exchange is a request followed by a response on a shared transport, so two
callers interleaving their requests each read the other's answer — and because
both are well-formed UDS frames, the result is a plausible *wrong* number
rather than an error.

`CarInterfaceService` therefore holds a reentrant lock that serialises every
bus operation, and the telemetry loop yields entirely while an agent turn is
running: a diagnostic answer matters more than a gauge refreshing on time.

The lock is covered by tests that detect overlapping exchanges directly, and
those tests fail if the lock is removed.

### WebSocket protocol

Server frames: `hello`, `modules`, `telemetry`, `agent.status`, `agent.tool`,
`agent.message`, `approval.request`, `approval.resolved`, `error`, `pong`.

Client frames: `chat`, `approval.response`, `refresh`, `ping`.

Every frame is a flat `{"type": ...}` discriminated union — the shape a
TypeScript `switch` narrows most cleanly. `protocol.py` and
`frontend/src/types/protocol.ts` are the two halves; keep them in step.

A malformed frame is answered with an `error` and the connection stays open.
Dropping the socket would take the telemetry stream and any pending approval
down with it.

### Frontend

React + Vite + TypeScript, with `@react-three/fiber` for the chassis,
`framer-motion` for the gauges and the authorisation gesture, and Tailwind +
daisyUI for the surface. See `frontend/README.md` for the component map and the
design decisions behind it.

## Testing strategy

The simulator is a real UDS implementation, not a mock. Mocking `send_request`
would prove the code *called* something; it would not catch a wrong status-mask
byte, a mis-ordered DID echo, or a retry loop that mishandles NRC 0x78 -- the
bugs that only show up under a car with the engine running.

`unittest.mock` is used on top of that, for the narrow cases where a
*transport* failure needs simulating (a mismatched DID echo, a stale frame) and
for the J2534 and ELM327 libraries.

Fault injection makes the flaky-bus paths deterministic:

```python
ecm.inject_faults(drop_next=2)      # two silent timeouts, then fine
ecm.inject_faults(pending_next=3)   # three NRC 0x78, then the answer
ecm.inject_faults(busy_next=1)      # one NRC 0x21 busyRepeatRequest
```

One test is worth calling out: every simulator PID encoder is round-tripped
through the *production* decoder. If the two ever disagree, the live-data tests
would be validating a formula against itself, and that test fails instead.
