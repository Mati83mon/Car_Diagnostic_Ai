# Land Rover Freelander 2 (2010, 2.2 TD4)

Reference notes for the development target. Everything here is either
legislated standard (and marked as such), or community knowledge to be
confirmed against your own vehicle.

## The vehicle

| | |
|---|---|
| Model | Land Rover Freelander 2 (L359), 2010 facelift |
| Engine | 2.2 TD4, PSA/Ford **DW12** family |
| Transmission | 6-speed manual, or Aisin AWF21 6-speed automatic |
| Driveline | Haldex Gen4 rear coupling, Terrain Response |
| Diagnostics | ISO 15765-4 CAN, 500 kbit/s, 11-bit identifiers |

The engine being a PSA DW12 is diagnostically useful: it is shared with
Peugeot 407/508, Citroen C5, Ford Mondeo/S-Max and Volvo. When searching the
web, turning *off* the vehicle-context filter often surfaces better material,
because the same injector, turbo and swirl-flap failures have been discussed at
much greater length on Peugeot and Ford forums.

```
search_web("DW12 swirl flap failure symptoms", include_vehicle_context=False)
```

## Diagnostic addresses

**Legislated (ISO 15765-4) -- trustworthy:**

| Module | Request | Response |
|---|---|---|
| ECM (engine) | `0x7E0` | `0x7E8` |
| TCM (transmission) | `0x7E1` | `0x7E9` |
| Functional broadcast | `0x7DF` | `0x7E8`-`0x7EF` |

**Community-derived -- confirm with `majster-ai scan` before trusting:**

| Module | Request | Response | Notes |
|---|---|---|---|
| ABS / DSC | `0x760` | `0x768` | safety-critical, read only |
| RCM (airbags) | `0x737` | `0x73F` | safety-critical, read only |
| CJB (body) | `0x726` | `0x72E` | |
| IPC (cluster) | `0x720` | `0x728` | |
| Haldex | `0x731` | `0x739` | |
| Terrain Response | `0x733` | `0x73B` | |
| PBM (park brake) | `0x72B` | `0x72F` | safety-critical |
| HVAC | `0x7A3` | `0x7AB` | |
| PAM (parking aid) | `0x736` | `0x73E` | |

A module that does not answer is either not fitted to your car, asleep, or
sitting at a different address. `scan_modules` distinguishes the first two from
the third by telling you what *did* answer.

Record confirmed addresses in `data/modules.json`:

```json
[
  {
    "name": "ABS",
    "request_id": "0x760",
    "response_id": "0x768",
    "verified": true,
    "notes": "confirmed by scan, 2010 TD4 automatic, VIN SALFA2BB..."
  }
]
```

## Known weak points

Not exhaustive, but these are the ones that come up repeatedly on this engine
and give the agent useful priors.

### Swirl flaps (P2015, P2016)

The intake manifold runner actuator linkage wears at the plastic bushes. The
position sensor then reads out of range. Common enough that P2015 on a DW12
should raise suspicion of the linkage before the sensor.

### Turbo underboost (P0299)

Usually the variable-geometry actuator rod seizing rather than the
turbocharger itself. Worth checking before condemning an expensive part:

1. Read `MAP` and `BAROMETRIC_PRESSURE` at idle -- they should be close.
2. Read `MAP` under load -- it should rise well above barometric.
3. If it does not, inspect the actuator rod for free movement, then the boost
   hoses for splits.

A P0299 and a P2015 together are frequently one story, not two faults: both
are air-path problems and the swirl flap linkage sits in the same area.

### DPF (P2002, P242F, P2463, P244A/B)

Regeneration is inhibited below roughly 10% fuel level and by short-journey
use. A DPF code on a car used for short trips is often a usage problem rather
than a component failure -- worth establishing before any parts are bought.

### EGR (P0401, P0404)

Carbon fouling of the valve and cooler. `COMMANDED_EGR` against `EGR_ERROR` is
the useful pair to read.

### Haldex

The pre-charge pump filter is a service item, not a lifetime part. Neglecting
it is the usual cause of Haldex faults and lost rear drive.

## Live data worth reading

| Signal | PID | Idle, warm | What it tells you |
|---|---|---|---|
| `RPM` | `0x0C` | ~800 | |
| `COOLANT_TEMP` | `0x05` | 85-92 degC | `-40` means the sensor is unplugged |
| `MAP` | `0x0B` | ~100 kPa | Compare against barometric |
| `BAROMETRIC_PRESSURE` | `0x33` | ~100 kPa | The reference for boost |
| `MAF` | `0x10` | 4-7 g/s | Low suggests an intake restriction |
| `FUEL_RAIL_PRESSURE` | `0x23` | ~26 000 kPa | 260 bar at idle is normal |
| `MODULE_VOLTAGE` | `0x42` | 13.8-14.4 V | Below 13 V running = charging fault |
| `COMMANDED_EGR` | `0x2C` | varies | Pair with `EGR_ERROR` |
| `OIL_TEMP` | `0x5C` | ~90 degC | |

All of the above are legislated SAE J1979 PIDs: the identifiers *and* the
scaling are standard, which is why they ship marked `verified`.

## Manufacturer DIDs

Manufacturer-specific DIDs (service `0x22`) are proprietary and this project
ships none of them, because a DID that returns rail pressure on one ECU
returns something else entirely on another. Guessing would produce confident,
wrong numbers.

To characterise your own car, use the read-only escape hatch:

```bash
majster-ai live --json VIN                 # confirm you are talking to the right car
python main.py dtc --module ECM --json     # baseline
```

```python
# Probe a DID range and log what comes back. Read-only.
from majster_ai.mcp_servers.car_interface.service import CarInterfaceService

with CarInterfaceService() as car:
    for did in range(0x2C00, 0x2C40):
        result = car.read_did("ECM", f"{did:04X}")
        if result.get("ok"):
            print(f"{did:04X}: {result['raw']}  ascii={result['as_ascii']!r}")
```

Correlate the values against a known physical state (engine off vs idle,
cold vs warm) to work out the scaling, then record it in `data/signals.json`:

```json
[
  {
    "name": "DPF_DIFF_PRESSURE",
    "description": "DPF differential pressure",
    "unit": "mbar",
    "source": "uds_did",
    "identifier": "0x2C05",
    "data_length": 2,
    "scale": 0.1,
    "offset": 0,
    "min": -50,
    "max": 1000,
    "verified": true
  }
]
```

Entries default to `verified: false`, and the agent is instructed to flag any
unverified reading rather than present it as fact. Set `verified: true` only
once you have confirmed the scaling against a known state on your own car.

## Workshop manuals

Put PDFs in `data/manuals/` and run `majster-ai ingest`. Everything is indexed
and searched locally; nothing is uploaded. `data/manuals/` is gitignored,
because workshop manuals are copyrighted and are yours to source legally.
