"""Small local CLI. OAuth callbacks are read privately rather than shell arguments."""
import argparse
import getpass
import json
import sys

from .common import ENDPOINTS, result
from .plugin import make_service


def parser():
    p = argparse.ArgumentParser(prog="hermes-whoop", description="Private WHOOP connector for Hermes")
    p.add_argument("--data-dir", help="Private state directory, outside the source checkout")
    p.add_argument("--timezone", help="IANA timezone, default WHOOP_TIMEZONE or UTC")
    p.add_argument("--privacy", choices=["summary", "structured", "raw"])
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Check required environment and show the onboarding steps")
    auth = commands.add_parser("auth", help="Start OAuth, or finish by pasting the complete redirect URL")
    auth.add_argument("--complete", action="store_true")
    doctor = commands.add_parser("doctor", help="Print exact setup/recovery fixes")
    doctor.add_argument("--online", action="store_true")
    for command in ("status", "inventory", "capabilities", "self-heal", "revoke"):
        commands.add_parser(command)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--force", action="store_true")
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--kinds", nargs="+", choices=list(ENDPOINTS))
    fetch.add_argument("--days", type=int, default=7)
    fetch.add_argument("--start")
    fetch.add_argument("--end")
    backfill = commands.add_parser("backfill")
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end")
    backfill.add_argument("--chunk-days", type=int, default=30)
    backfill.add_argument("--max-chunks", type=int, default=12)
    backfill.add_argument("--no-resume", action="store_true")
    latest = commands.add_parser("latest")
    latest.add_argument("kind", choices=list(ENDPOINTS))
    latest.add_argument("--max-age-seconds", type=float, default=64800)
    latest.add_argument("--max-record-age-seconds", type=float)
    latest.add_argument("--expected-day")
    latest.add_argument("--no-refresh", action="store_true")
    latest.add_argument("--allow-stale", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("kind", choices=list(ENDPOINTS))
    listing.add_argument("--start")
    listing.add_argument("--end")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--refresh", action="store_true")
    get = commands.add_parser("get")
    get.add_argument("kind", choices=list(ENDPOINTS))
    get.add_argument("record_id")
    get.add_argument("--refresh", action="store_true")
    for command in ("summary", "trend"):
        summary = commands.add_parser(command)
        summary.add_argument("--days", type=int, default=1 if command == "summary" else 7)
        summary.add_argument("--end", help="Last local calendar date, YYYY-MM-DD")
        summary.add_argument("--no-refresh", action="store_true")
        if command == "trend":
            summary.add_argument("--kind", choices=["recovery", "sleep"], default="recovery")
            summary.add_argument("--compare-days", type=int, default=7)
    return p


def main(argv=None):
    args = vars(parser().parse_args(argv))
    command = args.pop("command")
    try:
        service = make_service(args.pop("data_dir"), timezone=args.pop("timezone"), privacy=args.pop("privacy"))
        if command == "auth":
            answer = service.complete_auth(getpass.getpass("Paste the full redirected URL (hidden): ")) if args["complete"] else service.begin_auth()
        elif command == "setup":
            answer = service.doctor()
            answer["data"]["steps"] = ["Create a WHOOP developer app with the registered callback URL.",
                "Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in your private environment.",
                "Run hermes-whoop auth; open the URL on any device, then run hermes-whoop auth --complete and paste the full redirect URL. Run hermes-whoop doctor --online."]
        else:
            if "no_refresh" in args:
                args["refresh_if_stale" if command == "latest" else "refresh"] = not args.pop("no_refresh")
            if "no_resume" in args:
                args["resume"] = not args.pop("no_resume")
            method = {"latest": "get_latest", "list": "list_records", "get": "get_record", "inventory": "status", "self-heal": "self_heal"}.get(command, command)
            answer = getattr(service, method)(**args)
    except (EOFError, KeyboardInterrupt):
        answer = result(error={"code": "cancelled", "recovery": "restart_command"})
    except Exception:
        answer = result(error={"code": "command_failed", "recovery": "check_configuration_and_private_data_dir"})
    print(json.dumps(answer, indent=2, allow_nan=False))
    return 0 if answer["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
