import argparse
import asyncio
import sys

from keymasq import __version__
from keymasq.cli.commands import (
    cancel_macro_cli,
    create_hardware,
    list_devices,
    list_macros_cli,
    list_profiles_cli,
    play_macro_cli,
    set_diagnostics_cli,
    set_profile_state_cli,
    test_device,
)
from keymasq.common.asyncio_runtime import ensure_uvloop


def main() -> None:
    ensure_uvloop()

    parser = argparse.ArgumentParser(
        prog="keymasq",
        description="Keymasq CLI - Key remapping tool",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    devices_parser = subparsers.add_parser("devices", help="List available devices")
    devices_parser.add_argument("-v", "--verbose", action="store_true", help="Show details")

    hardware_parser = subparsers.add_parser("hardware", help="Hardware configuration")
    hardware_parser.add_argument("action", choices=["create", "list"], help="Action to perform")
    hardware_parser.add_argument("--vid", help="Vendor ID (e.g., 046d)")
    hardware_parser.add_argument("--pid", help="Product ID (e.g., c08b)")

    test_parser = subparsers.add_parser("test", help="Test device events")
    test_parser.add_argument("device", help="Device path (e.g., /dev/input/event5)")

    macros_parser = subparsers.add_parser("macros", help="Macro commands")
    macros_sub = macros_parser.add_subparsers(dest="macros_command", required=True)

    macros_sub.add_parser("list", help="List available macros")

    play_parser = macros_sub.add_parser("play", help="Play a macro by name")
    play_parser.add_argument("name", help="Macro name")
    play_parser.add_argument("--speed", type=float, default=1.0, help="Playback speed")

    macros_sub.add_parser("cancel", help="Cancel running macro playback")

    diagnostics_parser = subparsers.add_parser("diagnostics", help="Toggle keymasqd diagnostics")
    diagnostics_parser.add_argument("state", choices=["on", "off"], help="Enable or disable")
    diagnostics_parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Logging interval in seconds when enabled",
    )

    profiles_parser = subparsers.add_parser("profiles", help="Profile management")
    profiles_sub = profiles_parser.add_subparsers(dest="profiles_command", required=True)

    profiles_sub.add_parser("list", help="Show devices and profiles overview")

    enable_parser = profiles_sub.add_parser("enable", help="Enable a profile")
    enable_parser.add_argument("profile_name", help="Profile name")

    disable_parser = profiles_sub.add_parser("disable", help="Disable a profile")
    disable_parser.add_argument("profile_name", help="Profile name")

    toggle_parser = profiles_sub.add_parser("toggle", help="Toggle profile enabled state")
    toggle_parser.add_argument("profile_name", help="Profile name")

    args = parser.parse_args()

    if args.command == "devices":
        asyncio.run(list_devices(args.verbose))
    elif args.command == "hardware":
        if args.action == "list":
            print("Hardware configs not yet implemented")
        elif args.action == "create":
            if not args.vid or not args.pid:
                print("Error: --vid and --pid required")
                sys.exit(1)
            create_hardware(args.vid, args.pid)
    elif args.command == "test":
        asyncio.run(test_device(args.device))
    elif args.command == "macros":
        if args.macros_command == "list":
            list_macros_cli()
        elif args.macros_command == "play":
            play_macro_cli(args.name, args.speed)
        elif args.macros_command == "cancel":
            cancel_macro_cli()
    elif args.command == "diagnostics":
        set_diagnostics_cli(args.state == "on", args.interval)
    elif args.command == "profiles":
        if args.profiles_command == "list":
            list_profiles_cli()
        elif args.profiles_command == "enable":
            set_profile_state_cli("enable_profile", args.profile_name)
        elif args.profiles_command == "disable":
            set_profile_state_cli("disable_profile", args.profile_name)
        elif args.profiles_command == "toggle":
            set_profile_state_cli("toggle_profile", args.profile_name)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
