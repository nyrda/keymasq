import argparse
import sys

from keymasq import __version__
from keymasq.cli.commands import (
    cancel_macro_cli,
    create_macro_cli,
    delete_macro_cli,
    list_macros_cli,
    list_profiles_cli,
    play_adhoc_cli,
    play_macro_cli,
    set_diagnostics_cli,
    set_profile_state_cli,
    status_cli,
    type_cli,
)
from keymasq.common.asyncio_runtime import ensure_uvloop


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _add_json_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print raw session response as JSON",
    )


def _docs_url() -> str:
    version = __version__.strip()
    docs_version = "master" if not version or "dev" in version else f"v{version.removeprefix('v')}"
    return f"https://keymasq.tools/docs/{docs_version}/CLI.md"


def main() -> None:
    ensure_uvloop()
    argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="keymasq",
        description="Keymasq CLI - Key remapping tool",
        epilog=f"Full CLI reference: {_docs_url()}",
    )
    parser.add_argument(
        "--json",
        dest="global_json",
        action="store_true",
        help="Print raw session response as JSON",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    status_parser = subparsers.add_parser("status", help="Show Keymasq runtime status")
    _add_json_output(status_parser)

    type_parser = subparsers.add_parser(
        "type",
        help="Type text using an ad-hoc macro",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Inline controls: <tab>, <enter>, <wait:MS>, <wait:MIN:MAX>\n"
            r"Use \< to type a literal <." "\n"
            'Example: keymasq type "user<tab><wait:100:250>password<enter>"\n'
            f"Full reference: {_docs_url()}"
        ),
    )
    type_parser.add_argument("text", nargs="*", help="Text to type; stdin is used when omitted")
    type_parser.add_argument("--down-ms", type=int, default=10, help="Key down duration")
    type_parser.add_argument("--pause-ms", type=int, default=20, help="Pause between characters")
    type_parser.add_argument(
        "--no-unicode",
        action="store_true",
        help="Fail on unsupported characters instead of using Linux Ctrl+Shift+U input",
    )
    type_parser.add_argument("--speed", type=_positive_float, default=1.0, help="Playback speed")
    type_parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the compiled macro JSON instead of playing it",
    )

    play_parser = subparsers.add_parser(
        "play",
        help="Play an ad-hoc macro",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Compact tokens: key_a, key_a:1, key_a:0, btn_left,\n"
            "move_abs:X:Y, move_rel:DX:DY, wait:MS, wait:MIN:MAX\n"
            'Example: keymasq play key_leftctrl:1 wait:20 key_c wait:20 key_leftctrl:0\n'
            f"Full reference: {_docs_url()}"
        ),
    )
    play_parser.add_argument(
        "--json",
        dest="input_json",
        action="store_true",
        help="Read canonical macro JSON instead of compact event tokens",
    )
    play_parser.add_argument("--speed", type=_positive_float, default=1.0, help="Playback speed")
    play_parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the compiled macro JSON instead of playing it",
    )
    play_parser.add_argument(
        "events",
        nargs="*",
        help="Compact event tokens or JSON payload; stdin is used when omitted",
    )

    macros_parser = subparsers.add_parser(
        "macros",
        help="Macro commands",
        epilog=f"Full reference: {_docs_url()}",
    )
    macros_sub = macros_parser.add_subparsers(dest="macros_command", required=True)

    macros_list_parser = macros_sub.add_parser("list", help="List available macros")
    _add_json_output(macros_list_parser)

    macros_create_parser = macros_sub.add_parser("create", help="Create a macro from JSON")
    macros_create_parser.add_argument("name", help="Macro name")
    macros_create_parser.add_argument(
        "json",
        nargs="*",
        help="Macro JSON payload; stdin is used when omitted",
    )
    macros_create_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing macro",
    )
    _add_json_output(macros_create_parser)

    macros_play_parser = macros_sub.add_parser("play", help="Play a macro by name")
    macros_play_parser.add_argument("name", help="Macro name")
    macros_play_parser.add_argument(
        "--speed",
        type=_positive_float,
        default=1.0,
        help="Playback speed",
    )
    _add_json_output(macros_play_parser)

    macros_cancel_parser = macros_sub.add_parser("cancel", help="Cancel running macro playback")
    _add_json_output(macros_cancel_parser)

    macros_delete_parser = macros_sub.add_parser("delete", help="Delete a macro")
    macros_delete_parser.add_argument("name", help="Macro name")
    _add_json_output(macros_delete_parser)

    diagnostics_parser = subparsers.add_parser("diagnostics", help="Toggle keymasqd diagnostics")
    diagnostics_parser.add_argument("state", choices=["on", "off"], help="Enable or disable")
    diagnostics_parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Logging interval in seconds when enabled",
    )
    _add_json_output(diagnostics_parser)

    profiles_parser = subparsers.add_parser("profiles", help="Profile management")
    profiles_sub = profiles_parser.add_subparsers(dest="profiles_command", required=True)

    profiles_list_parser = profiles_sub.add_parser(
        "list",
        help="Show devices and profiles overview",
    )
    _add_json_output(profiles_list_parser)

    enable_parser = profiles_sub.add_parser("enable", help="Enable a profile")
    enable_parser.add_argument("profile_name", help="Profile name")
    _add_json_output(enable_parser)

    disable_parser = profiles_sub.add_parser("disable", help="Disable a profile")
    disable_parser.add_argument("profile_name", help="Profile name")
    _add_json_output(disable_parser)

    toggle_parser = profiles_sub.add_parser("toggle", help="Toggle profile enabled state")
    toggle_parser.add_argument("profile_name", help="Profile name")
    _add_json_output(toggle_parser)

    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "global_json", False)) or bool(
        getattr(args, "json_output", False)
    )

    if args.command == "status":
        status_cli(json_output=json_output)
    elif args.command == "type":
        type_cli(
            args.text,
            down_ms=args.down_ms,
            pause_ms=args.pause_ms,
            speed=args.speed,
            use_unicode_input=not args.no_unicode,
            print_json=args.print_json,
            json_output=json_output,
        )
    elif args.command == "play":
        play_adhoc_cli(
            args.events,
            input_json=args.input_json,
            speed=args.speed,
            print_json=args.print_json,
            json_output=json_output,
        )
    elif args.command == "macros":
        if args.macros_command == "list":
            list_macros_cli(json_output=json_output)
        elif args.macros_command == "create":
            create_macro_cli(
                args.name,
                args.json,
                force=args.force,
                json_output=json_output,
            )
        elif args.macros_command == "play":
            play_macro_cli(args.name, args.speed, json_output=json_output)
        elif args.macros_command == "cancel":
            cancel_macro_cli(json_output=json_output)
        elif args.macros_command == "delete":
            delete_macro_cli(args.name, json_output=json_output)
    elif args.command == "diagnostics":
        set_diagnostics_cli(args.state == "on", args.interval, json_output=json_output)
    elif args.command == "profiles":
        if args.profiles_command == "list":
            list_profiles_cli(json_output=json_output)
        elif args.profiles_command == "enable":
            set_profile_state_cli("enable_profile", args.profile_name, json_output=json_output)
        elif args.profiles_command == "disable":
            set_profile_state_cli("disable_profile", args.profile_name, json_output=json_output)
        elif args.profiles_command == "toggle":
            set_profile_state_cli("toggle_profile", args.profile_name, json_output=json_output)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
