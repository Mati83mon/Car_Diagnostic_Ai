"""Command-line interface for Majster-AI / Car_Diagnostic_AI (CDA).

Installed under three names -- ``majster-ai``, ``cda`` and
``car-diagnostic-ai`` -- all of which run this module.

    majster-ai doctor              check configuration and the interface
    majster-ai chat                interactive diagnostic session
    majster-ai ask "..."           one question, one answer
    majster-ai dtc --module ECM    read fault codes
    majster-ai live RPM MAF        read live data
    majster-ai scan                discover which ECUs answer
    majster-ai clear --module ECM  erase codes (asks for approval)
    majster-ai ingest              (re)build the workshop-manual index
    majster-ai serve car_interface run one MCP server on stdio
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from majster_ai import PROJECT_NAME, TARGET_VEHICLE, __version__
from majster_ai.config import CanBackend, Settings, load_settings, set_settings
from majster_ai.errors import MajsterError
from majster_ai.logging_setup import configure_logging, get_logger, log_settings_banner

log = get_logger("cli")

#: Subcommand -> MCP server module.
SERVERS = {
    "car_interface": "majster_ai.mcp_servers.car_interface.server",
    "rag_workshop": "majster_ai.mcp_servers.rag_workshop.server",
    "web_search": "majster_ai.mcp_servers.web_search.server",
}


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))


def _car_service(settings: Settings) -> Any:
    from majster_ai.mcp_servers.car_interface.service import CarInterfaceService

    return CarInterfaceService(settings)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    """Report the effective configuration and probe the interface."""
    from majster_ai.agent.llm import describe_llm

    print(f"{PROJECT_NAME} v{__version__}  (Majster-AI / CDA)")
    print(f"Target vehicle: {TARGET_VEHICLE}\n")

    print("Configuration")
    for key, value in settings.describe().items():
        print(f"  {key:<20} {value}")

    print("\nLLM")
    for key, value in describe_llm(settings).items():
        print(f"  {key:<20} {value}")

    print("\nVehicle interface")
    if settings.can_backend.is_physical:
        print(f"  Backend {settings.can_backend.value!r} will open real hardware.")
    with _car_service(settings) as car:
        status = car.status()
        print(f"  safety_mode        {status['safety_mode']}")
        print(
            f"  modules known      {status['modules_known']} "
            f"({status['modules_verified']} verified)"
        )
        print(f"  signals known      {status['signals_known']}")
        if args.probe:
            print("\n  Probing modules...")
            scan = car.scan_modules()
            for entry in scan["responding"]:
                print(f"    [OK]     {entry['module']:<8} {entry['address']}")
            for entry in scan["not_responding"]:
                name = entry.get("module", "?")
                print(f"    [silent] {name:<8} {entry.get('address', '')}")
            print(f"  {scan['summary']}")

    print("\nWorkshop manuals")
    try:
        from majster_ai.mcp_servers.rag_workshop.service import RagService

        rag_status = RagService(settings).status()
        print(f"  manuals_dir        {rag_status['manuals_dir']}")
        print(f"  documents present  {len(rag_status['documents_available'])}")
        print(f"  chunks indexed     {rag_status['indexed']}")
        print(
            f"  embeddings         {rag_status['embedding_backend']['backend']} "
            f"({'semantic' if rag_status['embedding_backend']['semantic'] else 'lexical'})"
        )
    except Exception as exc:
        print(f"  unavailable: {exc}")

    print("\nWeb search")
    try:
        from majster_ai.mcp_servers.web_search.service import WebSearchService

        for provider in WebSearchService(settings).status()["providers"]:
            state = "available" if provider["available"] else "unavailable"
            print(f"  {provider['name']:<18} {state}")
    except Exception as exc:
        print(f"  unavailable: {exc}")

    if settings.is_read_only:
        print("\nSafety: READ_ONLY. No write can reach the vehicle.")
    else:
        print(
            "\nSafety: WRITES ENABLED. Every write still requires your approval."
            if settings.require_approval
            else "\nSafety: WRITES ENABLED AND UNATTENDED. Bench rigs only!"
        )
    return 0


def cmd_dtc(args: argparse.Namespace, settings: Settings) -> int:
    """Read fault codes."""
    with _car_service(settings) as car:
        result = car.read_all_dtcs() if args.all else car.read_dtc(args.module, args.status)
    if args.json:
        _print_json(result)
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        print(f"error: {result.get('message', result.get('error'))}")
        return 1

    if args.all:
        for entry in result["results"]:
            if entry.get("ok") and entry.get("count"):
                print(f"\n{entry['module']} ({entry['address']}):")
                for dtc in entry["dtcs"]:
                    print(f"  {dtc['full_code']:<12} {dtc['description'][:64]}")
        print(f"\n{result['summary']}")
        return 0

    print(f"{result['module']} ({result['address']}) - {result['count']} code(s)")
    for dtc in result["dtcs"]:
        flags = ",".join(dtc["status"]["flags"][:3])
        print(f"  {dtc['full_code']:<12} {dtc['description'][:56]}")
        print(f"  {'':<12} status: {flags}")
    print(f"\n{result['summary']}")
    return 0


def cmd_live(args: argparse.Namespace, settings: Settings) -> int:
    """Read live data."""
    with _car_service(settings) as car:
        result = car.read_live_data(args.signals, module_id=args.module)
    if args.json:
        _print_json(result)
        return 0 if result.get("ok") else 1
    for value in result.get("values", []):
        unit = f" {value['unit']}" if value["unit"] else ""
        print(f"  {value['signal']:<24} {value['value']}{unit}")
        if value.get("warning"):
            print(f"  {'':<24} ! {value['warning']}")
    for failure in result.get("failures", []):
        print(f"  {failure.get('requested'):<24} unavailable: {failure.get('message', '')[:60]}")
    return 0 if result.get("ok") else 1


def cmd_scan(args: argparse.Namespace, settings: Settings) -> int:
    """Discover which modules answer."""
    with _car_service(settings) as car:
        result = car.scan_modules()
    if args.json:
        _print_json(result)
        return 0
    for entry in result["responding"]:
        mark = "verified" if entry["previously_verified"] else "NEW - record this"
        print(f"  [OK]     {entry['module']:<8} {entry['address']}  ({mark})")
    for entry in result["not_responding"]:
        print(f"  [silent] {entry.get('module', '?'):<8} {entry.get('address', '')}")
    print(f"\n{result['summary']}\n{result['next_step']}")
    return 0


def cmd_clear(args: argparse.Namespace, settings: Settings) -> int:
    """Clear fault codes, with the full approval handshake."""
    from majster_ai.agent.hitl import ApprovalRequest, ConsoleApprover, assess_risk

    if settings.is_read_only:
        print(
            "Refused: the agent is READ_ONLY. Set MAJSTER_WRITE_ENABLED=true in "
            "your .env to permit writes -- and read docs/SAFETY.md first."
        )
        return 2

    with _car_service(settings) as car:
        preview = car.clear_dtc(module_id=args.module, dtc_code=args.code)
        if not preview.get("requires_confirmation"):
            _print_json(preview)
            return 0 if preview.get("ok") else 1

        impact = preview["impact"]
        arguments = {"module_id": args.module, "dtc_code": args.code}
        request = ApprovalRequest(
            tool_name="clear_dtc",
            arguments=arguments,
            impact=impact,
            confirmation_token=preview["confirmation_token"],
            risk=assess_risk("clear_dtc", arguments, impact),
        )
        decision = ConsoleApprover().request(request)
        if not decision.approved:
            print("Nothing was written.")
            return 3

        result = car.clear_dtc(
            module_id=args.module,
            dtc_code=args.code,
            confirmation_token=preview["confirmation_token"],
        )
    print(result.get("summary", result.get("message", "")))
    return 0 if result.get("ok") else 1


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    """Build the workshop-manual index."""
    from majster_ai.mcp_servers.rag_workshop.service import RagService

    result = RagService(settings).ingest(rebuild=args.rebuild)
    if args.json:
        _print_json(result)
    else:
        print(result.get("summary", result.get("message", "")))
        if result.get("ok"):
            for name in result["files_indexed"]:
                print(f"  - {name}")
    return 0 if result.get("ok") else 1


def cmd_search(args: argparse.Namespace, settings: Settings) -> int:
    """Search the indexed manuals."""
    from majster_ai.mcp_servers.rag_workshop.service import RagService

    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    result = RagService(settings).search_manual(query, top_k=args.top_k)
    if args.json:
        _print_json(result)
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        print(f"error: {result.get('message')}")
        return 1
    for entry in result["results"]:
        print(f"\n[{entry['score']:.3f}] {entry['citation']}")
        print(f"  {entry['text'][:400]}")
    print(f"\n{result['summary']}")
    return 0


def cmd_ask(args: argparse.Namespace, settings: Settings) -> int:
    """Ask one question and print the answer."""
    from majster_ai.agent.runner import DiagnosticSession

    with DiagnosticSession(settings=settings) as session:
        result = session.ask(" ".join(args.question))
        print(result.answer)
        if result.tools_used:
            print(f"\n(tools: {', '.join(result.tools_used)})", file=sys.stderr)
    return 0


def cmd_chat(args: argparse.Namespace, settings: Settings) -> int:
    """Interactive diagnostic session."""
    from majster_ai.agent.runner import run_console

    return run_console(settings=settings)


def cmd_web(args: argparse.Namespace, settings: Settings) -> int:
    """Serve the Cyber-HUD web interface."""
    from majster_ai.web.app import FRONTEND_DIST, run

    if not FRONTEND_DIST.is_dir():
        print(
            "note: the frontend has not been built yet. The API will run, but "
            "there is no UI to serve.\n"
            "      Build it with:  cd frontend && npm install && npm run build\n"
            "      Or run the Vite dev server alongside:  npm run dev\n",
            file=sys.stderr,
        )

    scheme = f"http://{args.host}:{args.port}"
    print(f"{PROJECT_NAME} Cyber-HUD -> {scheme}")
    print(f"  interface : {settings.can_backend.value} ({settings.can_channel})")
    print(f"  safety    : {settings.safety_mode.value.upper()}")
    if not settings.can_backend.is_physical:
        print("  NOTE      : simulated vehicle - readings are synthetic.")
    print()
    run(host=args.host, port=args.port, settings=settings, reload=args.reload)
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    """Run one MCP server on stdio."""
    import importlib

    module = importlib.import_module(SERVERS[args.server])
    return int(module.main())


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="majster-ai",
        description=(
            f"{PROJECT_NAME} (CDA / Majster-AI) - automotive UDS/CAN diagnostic "
            f"agent. Target vehicle: {TARGET_VEHICLE}."
        ),
        epilog="Read docs/SAFETY.md before connecting to a real vehicle.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", help="Path to a .env file (default: ./.env)")
    parser.add_argument("--log-level", help="CRITICAL|ERROR|WARNING|INFO|DEBUG")
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in CanBackend],
        help="Override the vehicle interface backend for this run.",
    )
    parser.add_argument("--channel", help="Override the interface channel.")

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check configuration and the interface")
    doctor.add_argument(
        "--probe", action="store_true", help="Also probe every ECU address (read-only)"
    )
    doctor.set_defaults(func=cmd_doctor)

    dtc = sub.add_parser("dtc", help="Read fault codes")
    dtc.add_argument("-m", "--module", default="ECM")
    dtc.add_argument(
        "-s",
        "--status",
        default="all",
        help="all|confirmed|pending|test_failed|confirmed_or_pending",
    )
    dtc.add_argument("--all", action="store_true", help="Scan every module")
    dtc.add_argument("--json", action="store_true")
    dtc.set_defaults(func=cmd_dtc)

    live = sub.add_parser("live", help="Read live data")
    live.add_argument("signals", nargs="+", help="e.g. RPM COOLANT_TEMP MAF")
    live.add_argument("-m", "--module", default="ECM")
    live.add_argument("--json", action="store_true")
    live.set_defaults(func=cmd_live)

    scan = sub.add_parser("scan", help="Discover which ECUs answer")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    clear = sub.add_parser("clear", help="Erase fault codes (asks for approval)")
    clear.add_argument("-m", "--module", default="ECM")
    clear.add_argument("-c", "--code", help="A single code, e.g. P0299")
    clear.set_defaults(func=cmd_clear)

    ingest = sub.add_parser("ingest", help="Build the workshop-manual index")
    ingest.add_argument("--rebuild", action="store_true", help="Drop the index first")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    search = sub.add_parser("search", help="Search the indexed manuals")
    # nargs="+" so an unquoted multi-word query works, matching `ask`.
    search.add_argument("query", nargs="+", help="What to look for")
    search.add_argument("-k", "--top-k", type=int, default=5)
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    ask = sub.add_parser("ask", help="Ask the agent one question")
    ask.add_argument("question", nargs="+")
    ask.set_defaults(func=cmd_ask)

    chat = sub.add_parser("chat", help="Interactive diagnostic session")
    chat.set_defaults(func=cmd_chat)

    web = sub.add_parser("web", help="Serve the Cyber-HUD web interface")
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Anything beyond localhost exposes the diagnostic "
        "API to your network - see docs/SAFETY.md.",
    )
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--reload", action="store_true", help="Auto-reload on code changes")
    web.set_defaults(func=cmd_web)

    serve = sub.add_parser("serve", help="Run one MCP server on stdio")
    serve.add_argument("server", choices=sorted(SERVERS))
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.backend:
        overrides["can_backend"] = args.backend
    if args.channel:
        overrides["can_channel"] = args.channel

    try:
        settings = load_settings(args.env_file, **overrides)
    except MajsterError as exc:
        print(f"configuration error: {exc.message}", file=sys.stderr)
        return 2

    # Make the resolved settings process-wide, so services constructed deeper
    # in the call stack see the CLI overrides too.
    set_settings(settings)

    configure_logging(settings, force=True)
    if settings.log_level == "DEBUG":
        log_settings_banner(settings)

    try:
        return int(args.func(args, settings))
    except MajsterError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
