# Safety

**Read this before connecting anything to a vehicle you care about.**

This software can transmit on a car's CAN bus. A diagnostic bus is not a
read-only debug port: it is the network the engine, brakes, transmission and
restraints talk on. Frames sent to the wrong address, or the right address at
the wrong moment, can set fault codes, disable systems, or -- with the wrong
service -- corrupt a control module's memory badly enough that it needs
replacing.

None of that is theoretical. It is why this project is built the way it is.

---

## The threat model

The specific risk of an *LLM-driven* diagnostic tool is not that the model is
malicious. It is that a language model is agreeable, and a plausible chain of
reasoning can end at "so I'll just clear the codes and see if they come back."
That is a reasonable-sounding sentence and a bad idea on a car mid-diagnosis.

So the design assumption is: **the model will eventually try to write to the
vehicle when it should not.** Every safeguard below is built to hold when that
happens, without relying on the model's judgement.

---

## The four layers

A write has to get through all four. They are independent on purpose: a prompt
can be argued with, a graph can be bypassed by calling the MCP server directly,
so the guarantee cannot live in only one place.

### 1. The master switch (configuration)

`MAJSTER_WRITE_ENABLED` defaults to `false`. While it is false the service
refuses every write outright, issues no approval token, and never prompts. The
agent cannot change this setting -- it is read from the environment at startup
and there is no tool that writes it.

```
MAJSTER_WRITE_ENABLED=false   # the shipped default
```

### 2. The token handshake (service layer)

With writes enabled, a mutating call still never executes on first request.
The service returns a refusal carrying:

* exactly which DTCs would be erased,
* the risks of erasing them,
* a single-use `confirmation_token`.

The token is generated server-side, expires after five minutes, and is bound
to a hash of the operation's arguments. Concretely:

* A token issued for "clear the ECM" **cannot** clear the ABS module.
* A token issued for "clear P0299" **cannot** clear all codes.
* A token the model invented is refused.
* A token already used is refused.

This layer lives in `CarInterfaceService`, which is the only path to the bus.
It therefore holds for *any* MCP client, not just this agent.

### 3. The human pause (orchestrator)

The LangGraph `tools` node detects a write, performs a side-effect-free dry
run to obtain the impact summary, and then calls `interrupt()`. Execution
stops. A human sees what would happen and decides.

Only an explicit, unambiguous approval proceeds. Everything else -- a refusal,
an empty answer, a closed stdin, a crashed approval UI, an unrecognised resume
value, a non-interactive session -- is treated as **no**.

The dry run is deliberately free of side effects because LangGraph re-runs a
node from the top when it resumes. Reading DTCs twice is harmless; that is
precisely why the clear cannot happen until after the pause.

### 4. The prompt (model behaviour)

The system prompt tells the model what the other three layers will do, so its
behaviour matches the guarantees instead of fighting them. This layer is the
weakest and is treated as such: it makes the agent *pleasant*, not *safe*.

---

## What the operator sees

```
========================================================================
  WRITE OPERATION - HUMAN APPROVAL REQUIRED
========================================================================
  Operation : clear_dtc
  Module    : ECM (Engine Control Module - 2.2 TD4)
  Address   : 0x7E0
  Scope     : ALL stored DTCs in this module
  Risk      : MEDIUM
  Reversible: NO
  Will erase 3 code(s):
      - P0299-00     Turbocharger/Supercharger A Underboost Condition
      - P2015-00     Intake Manifold Runner Position Sensor/Switch Circuit
      - P0401-00     Exhaust Gas Recirculation Flow Insufficient Detected

  Consequences:
      ! Freeze-frame data captured when the fault occurred will be lost.
        That data is often the most useful evidence you have.
      ! Readiness monitors reset. The vehicle may fail an emissions test
        until a full drive cycle completes.
      ! Clearing does not repair anything. If the fault is still present
        the code will return.
========================================================================

  Type 'yes' to authorise, anything else to decline:
```

You must type `yes` in full. `y` is not enough, and neither is silence.

---

## Why clearing codes is rarely the right answer

Worth stating plainly, because it is the operation people reach for first:

* **It destroys evidence.** Freeze-frame data -- the sensor snapshot from the
  moment the fault occurred -- is frequently the single most useful thing you
  have, and clearing deletes it.
* **It resets readiness monitors.** The car may fail an emissions test until a
  complete drive cycle has run.
* **It repairs nothing.** A fault whose cause is still present returns.

Clear codes *after* a repair, to confirm the fix. Not before, to see what
happens.

---

## Modules to leave alone

`RCM` (restraints/airbags), `ABS` (brakes) and `PBM` (electric park brake) are
classified as safety-critical. Writes there are marked HIGH risk and carry an
extra warning, because clearing a fault in one of those systems can hide a
genuine defect in something that has to work when you need it.

Read from them freely. Do not write to them unless the repair is finished.

---

## Unverified addresses

Only two diagnostic addresses on any vehicle are legislated and therefore
certain: the powertrain addresses `0x7E0`/`0x7E8` and `0x7E1`/`0x7E9`
(ISO 15765-4). Everything else in the built-in Freelander 2 module map is
community-derived and marked `verified: false`.

An unverified address that happens to belong to a different module is exactly
how people write to something they did not intend to. Before trusting one:

```bash
majster-ai scan
```

`scan_modules` probes each address with TesterPresent -- a harmless presence
check -- and reports which ones actually answer on *your* car. Record the
confirmed ones in `data/modules.json` with `"verified": true`.

The same applies to live-data scaling. Manufacturer DIDs are proprietary; a
signal marked `verified_scaling: false` may be returning a correct number with
the wrong units, or an entirely different quantity.

---

## Before you connect to a real car

1. Run everything against the simulator first (`MAJSTER_CAN_BACKEND=virtual`,
   the default). Get familiar with what the agent does.
2. Battery voltage matters. Diagnostics on a weak battery cause spurious codes
   across every module. Put a charger on it for anything longer than a few
   minutes.
3. Ignition on, engine off (position II) is the right state for most reads.
4. Do not diagnose while driving. Do not let anyone else drive while you are
   connected.
5. Keep `MAJSTER_WRITE_ENABLED=false` until you have a specific reason to
   change it, and set it back afterwards.

---

## Disclaimer

This project is for **educational and research purposes**. It is not a
certified diagnostic tool and it is not a substitute for the manufacturer's
equipment or a qualified technician.

The authors accept no liability for damage to a vehicle, damage to control
modules, failed repairs, voided warranties, or injury arising from the use of
this software. You are responsible for understanding what any command does
before it reaches your car's bus.

If you are not certain what a command does, do not send it.

---

## Zrzeczenie się odpowiedzialności (PL)

Projekt służy **wyłącznie do celów edukacyjnych i badawczych**. Autor nie
ponosi odpowiedzialności za jakiekolwiek uszkodzenia pojazdu, uszkodzenia
sterowników (ECU) lub obrażenia ciała wynikające z użytkowania tego
oprogramowania.

Zawsze upewnij się, że wiesz, jakie komendy -- zwłaszcza polecenia ZAPISU --
są wysyłane na magistralę CAN Twojego samochodu.
