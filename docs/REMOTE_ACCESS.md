# Running the stack on one machine and using it from another

The usual reason to want this: the laptop that goes out to the car is the one
you cannot install anything on — no disk space, wrong OS, locked down — while
the machine with room to spare is a desktop that never leaves the house.

Majster-AI is a web application, so splitting it is straightforward. The part
that needs care is the vehicle interface, because that is the one thing that
cannot move over the network by itself.

## The short version

```bash
# on the machine that runs everything (Linux, plenty of disk)
majster-ai web --host 0.0.0.0 --port 8000
```

Then open `http://<that-machine's-IP>:8000` in a browser on the other machine.
That is all the UI needs. The page, the 3D model, the REST endpoints and the
`/ws/diagnostics` WebSocket are all served from the same origin and the same
port, so nothing else has to be configured, forwarded or proxied.

The frontend builds its WebSocket URL from `window.location.host`, so it
follows whatever address you typed. There is no hard-coded `localhost` to
change.

## Which machine needs what

| | Runs the Python stack | Runs the browser | Needs the OBD adapter |
| --- | --- | --- | --- |
| Desktop (Linux) | yes | optional | **yes** |
| Laptop at the car | no | yes | no, unless bridging |

The browser is a thin client. It draws the HUD and sends your answers to the
approval prompt; it never touches the CAN bus. Everything that talks to the
car happens in the Python process.

**This is the constraint that decides your setup: the adapter has to be
reachable from the machine running `majster-ai web`, not from the machine
showing the UI.** Moving the browser is free. Moving the vehicle interface is
the part that needs a plan.

## Getting the interface to the machine that runs the stack

Four options, best first for a stationary desktop.

### 1. WiFi ELM327 — no second computer at all

The cheap WiFi OBD dongles expose the adapter as a TCP socket instead of a
serial port, usually on `192.168.0.10:35000`, either as their own access point
or joined to your network. Point the channel at it:

```bash
MAJSTER_CAN_BACKEND=rfcomm
MAJSTER_CAN_CHANNEL=socket://192.168.0.10:35000
```

No laptop in the car, nothing installed anywhere else. The limit is WiFi range
between the car and the dongle's host network, and these adapters vary wildly
in quality — a bad one drops frames under load and the retry logic will spend
its budget papering over it.

### 2. Bluetooth ELM327 — if the car parks close

Pair the adapter with the Linux box and bind it to an RFCOMM node:

```bash
bluetoothctl            # scan, pair, trust the adapter
sudo rfcomm bind 0 <ADAPTER_MAC> 1
MAJSTER_CAN_BACKEND=rfcomm
MAJSTER_CAN_CHANNEL=/dev/rfcomm0
```

Class 2 Bluetooth is about 10 m in free air and less through a wall and a car
body. Fine for a car on the drive, not for one down the street.

### 3. Adapter plugged straight into the Linux box

If the car can be parked within cable reach, this is the most reliable option
and the one with the fewest moving parts. A Tactrix Openport over J2534, or a
CANable/USB2CAN over SocketCAN:

```bash
MAJSTER_CAN_BACKEND=socketcan
MAJSTER_CAN_CHANNEL=can0
```

SocketCAN is Linux-native, so a Linux desktop is a better host for it than the
Windows laptop ever was.

### 4. Bridge from the laptop that stays in the car

Keep the laptop in the car as nothing more than a wire. It runs a small TCP
bridge — no Python stack, no disk space — and the desktop connects to it:

```bash
# on the laptop, next to the car (needs socat, or ser2net, or com0com on Windows)
socat TCP-LISTEN:35000,reuseaddr,fork /dev/ttyUSB0,raw,b115200
```

```bash
# on the desktop
MAJSTER_CAN_CHANNEL=socket://<laptop-IP>:35000
```

The laptop needs a network path to the desktop, which in a driveway usually
means a phone hotspot on both. This keeps a J2534/USB adapter that only has
Windows drivers usable while the stack itself runs on Linux.

> The `socket://` forms above go through pyserial's URL handler, so they work
> anywhere a device path works. They carry the ELM327 protocol, not raw CAN —
> the `j2534` backend needs its shared library in the same process and cannot
> be bridged this way.

## Finding the address

```bash
hostname -I | awk '{print $1}'     # first address on the box
ip -brief address                  # all of them, per interface
```

Use the LAN address (typically `192.168.x.x`), not `127.0.0.1` — that one
means "this machine" and will not resolve from the laptop.

If the desktop's address changes between reboots, give it a DHCP reservation on
the router, or the URL will move under you.

## Firewall

Most desktop Linux installs do not filter inbound connections by default, but
if `ufw` is enabled the port has to be opened:

```bash
sudo ufw status
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
```

Scope the rule to your own subnet rather than opening the port to everything.

## Before you expose it

`majster-ai web` binds to `127.0.0.1` unless told otherwise, and logs a warning
when you override that. The warning is not boilerplate:

```
Binding to 0.0.0.0 exposes the diagnostic API beyond this machine. Anything
that can reach it can ask the agent to propose a vehicle write, and the
approval prompt would be answered by whoever is there.
```

There is no authentication on the interface. The safety model still holds — the
agent is read-only by default, a write needs a confirmation token the server
issues and the browser never sees, and the drag-to-authorize gesture has to be
completed by a person — but *whoever has the page* is that person. On a home
network behind a router that is a reasonable trade. Do not put it on a public
address, a café network, or a port forward.

If you need it across the internet, tunnel it rather than exposing it:
WireGuard, Tailscale, or `ssh -L 8000:127.0.0.1:8000 user@desktop` and browse
to `http://127.0.0.1:8000` on the laptop. That way the port stays bound to
localhost and the authentication is the SSH key you already have.

## When it does not work

**Page loads, but the HUD stays empty and says it is connecting.** The
WebSocket is not getting through. HTTP and WebSocket use the same port here, so
if the page loaded the port is open — suspect something between the two
machines that proxies HTTP but drops upgrade requests. A corporate proxy, a
captive portal, or a `HTTP_PROXY` set in the browser's environment will all do
this. Check the browser console for the failed `ws://` connection and try
without the proxy.

**Page does not load at all.** Check what the server is actually bound to:

```bash
ss -tlnp | grep 8000
```

`127.0.0.1:8000` means the `--host 0.0.0.0` flag did not take effect.
`0.0.0.0:8000` means it is listening on every interface, and the problem is
further out — firewall, wrong address, or the two machines are not on the same
network.

**The car side reports no interface.** That is the adapter, not the network.
Run the built-in check on the machine that runs the stack:

```bash
majster-ai doctor
```

It reports which backend is configured, whether the channel opens, and what it
found on the bus.
