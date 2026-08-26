# Hardware and platform setup

The software talks to a car through one of five backends. Pick the one that
matches the interface you own, set `MAJSTER_CAN_BACKEND` accordingly, and use
`majster-ai doctor --probe` to confirm it works before trusting it.

| Backend | Interface | Platform | ISO-TP handled by |
|---|---|---|---|
| `virtual` | none | anywhere | n/a (simulated) |
| `socketcan` | USB2CAN, CANable, PiCAN | Linux with a real kernel | `can-isotp` |
| `slcan` | CANable/CANtact in slcan firmware | Linux, macOS, Windows | `can-isotp` |
| `serial` | python-can serial devices | any | `can-isotp` |
| `j2534` | Tactrix Openport 2.0 and compatibles | Linux, Windows | the interface |
| `rfcomm` | ELM327 / OBDLink over Bluetooth | Linux, **Termux** | the adapter |

---

## Tactrix Openport 2.0 (J2534) -- recommended

The most capable option: genuine J2534 PassThru, and the ISO15765 protocol
means segmentation, flow control and reassembly happen *inside the interface*
rather than in a Python timing loop. On a Raspberry Pi or a phone that
difference is the difference between working and not.

```bash
MAJSTER_CAN_BACKEND=j2534
MAJSTER_J2534_LIBRARY=/usr/local/lib/libop20pt32.so
```

On Windows the library is typically:

```
C:\Program Files (x86)\OpenECU\OpenPort 2.0\drivers\op20pt32.dll
```

**The library's word size must match your Python.** A 32-bit `op20pt32.dll`
will not load into 64-bit Python, and the resulting `OSError` is unhelpful --
this is by far the most common J2534 setup problem, so the error message says
so explicitly.

If multi-frame reads (DTC lists) fail while short reads work, the flow-control
filter is missing. This project installs one on `open()`; if you are debugging
a different tool, that is the first thing to check.

---

## SocketCAN (USB2CAN, CANable, PiCAN)

Bring the interface up before running anything -- SocketCAN takes its bit rate
from the kernel, not from the application:

```bash
sudo ip link set can0 up type can bitrate 500000
ip -details link show can0        # confirm
```

Then:

```bash
MAJSTER_CAN_BACKEND=socketcan
MAJSTER_CAN_CHANNEL=can0
```

### A virtual bus for development

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

`vcan0` gives you a real SocketCAN interface with nothing on the other end --
useful for exercising the driver path. For actual test *data*, the built-in
simulator (`MAJSTER_CAN_BACKEND=virtual`) is more useful because it answers.

**SocketCAN is not available on stock Termux.** It needs kernel modules that a
non-rooted Android device does not have. Use `rfcomm` instead.

---

## Bluetooth ELM327 (the Termux path)

A phone cannot open a SocketCAN interface without root, but it can open
`/dev/rfcomm0`. For many people this is the only way the project runs at all.

Pair the adapter, then bind it:

```bash
bluetoothctl              # scan on / pair <MAC> / trust <MAC>
sudo rfcomm bind 0 <MAC> 1
ls -l /dev/rfcomm0
```

```bash
MAJSTER_CAN_BACKEND=rfcomm
MAJSTER_CAN_CHANNEL=/dev/rfcomm0
```

### Choosing an adapter

This matters more than it should. `AT CAF1` (CAN auto-formatting) makes the
adapter perform ISO-TP itself, which is what allows UDS to work over an ELM327
at all. Cheap clones frequently report v1.5 while implementing a subset of
v1.3, and a common failure is exactly this: single-frame reads work, DTC lists
(which are multi-frame) return nothing.

* **OBDLink MX+ / LX** -- genuine ST chipset, reliable for UDS. Worth the money.
* **Genuine ELM327 v1.4b+** -- works.
* **Sub-EUR 10 clones** -- may work, may silently mangle multi-frame responses.

The transport tolerates an adapter rejecting the optional flow-control
commands (`ATFCSH`, `ATFCSD`, `ATFCSM`) and falls back to `CAF1`'s automatic
handling. It does *not* tolerate a rejected `ATCAF1` or `ATE0`, because
without those nothing downstream can work -- and it says so rather than
failing obscurely later.

---

## Termux specifics

```bash
pkg install python python-pip clang libffi openssl rust
pip install --upgrade pip

git clone https://github.com/Mati83mon/Car_Diagnostic_Ai.git
cd Car_Diagnostic_Ai

# Skip the heavy extras; they are optional by design.
pip install -e ".[car,mcp,agent,web]"
```

`chromadb` and `sentence-transformers` are the two entries most likely to
refuse to build on ARM. You do not need either:

* Without `sentence-transformers`, the RAG server uses a dependency-free
  lexical hash embedder. Retrieval matches shared wording rather than meaning,
  and `search_manual` says so in every result.
* Without `chromadb`, the index is a JSON file scanned linearly. At workshop-
  manual scale that is perfectly fast.

For the LLM, either use `ANTHROPIC_API_KEY` over mobile data, or run Ollama on
a machine on the same network and point at it:

```bash
MAJSTER_OLLAMA_BASE_URL=http://192.168.1.10:11434
```

### XFCE on Termux

If you are running the Termux XFCE desktop, nothing changes -- the CLI and the
agent are terminal programs. Serial permissions are the usual sticking point:
`/dev/rfcomm0` must be readable by your user.

---

## Raspberry Pi

A Pi with a PiCAN2/PiCAN3 HAT is a good permanent installation. Add to
`/boot/config.txt`:

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

Then bring `can0` up as above. Set `MAJSTER_LOG_FILE` so you have a record of
what was sent.

---

## Wiring to the vehicle

The Freelander 2's OBD-II socket is in the driver's footwell, above the
pedals. Diagnostic CAN is on the standard pins:

| Pin | Signal |
|---|---|
| 6 | CAN High |
| 14 | CAN Low |
| 16 | Battery + (permanent) |
| 4, 5 | Ground |

500 kbit/s, 11-bit identifiers, ISO 15765-4. That is what
`MAJSTER_CAN_BITRATE=500000` and ELM protocol `SP6` mean.

**Do not fit your own termination.** The vehicle's bus is already terminated;
adding a 120R resistor at the OBD port loads the bus and causes errors that
look like intermittent module faults.

---

## Battery

Long diagnostic sessions with the ignition on and the engine off will flatten
a battery, and a low battery *causes* fault codes across every module on the
car -- you will chase faults that are not there. Put a charger or a stable
supply on it for anything beyond a few minutes.

---

## Verifying the setup

```bash
majster-ai doctor --probe
```

```
Vehicle interface
  safety_mode        read_only
  modules known      11 (2 verified)

  Probing modules...
    [OK]     ECM      0x7E0
    [OK]     TCM      0x7E1
    [silent] ABS      0x760
    ...
```

`[OK]` means something answered at that address on your car. `[silent]` means
either the module is not fitted, or -- for the addresses marked unverified --
the address is simply wrong. Record what answers in `data/modules.json` with
`"verified": true` and future runs will trust it.
