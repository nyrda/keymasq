import sys
from math import isfinite

from keymasq import __version__
from keymasq.common.macro_compile import (
    DEFAULT_TYPE_MACRO_DOWN_MS,
    DEFAULT_TYPE_MACRO_PAUSE_MS,
)


def _positive_float(value: str) -> float:
    import argparse

    return _parse_positive_float(value, argparse.ArgumentTypeError)


def _parse_positive_float(value: str, error_type: type[Exception] = ValueError) -> float:
    parsed = float(value)
    if not isfinite(parsed) or parsed <= 0:
        raise error_type("must be a finite number greater than 0")
    return parsed


def _add_json_output(parser, help_text: str = "Print raw session response as JSON") -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help=help_text,
    )


def _docs_url() -> str:
    version = __version__.strip()
    docs_version = "master" if not version or "dev" in version else f"v{version.removeprefix('v')}"
    return f"https://keymasq.tools/docs/{docs_version}/cli/"


def _type_controls_docs_url() -> str:
    version = __version__.strip()
    docs_version = "master" if not version or "dev" in version else f"v{version.removeprefix('v')}"
    return f"https://keymasq.tools/docs/{docs_version}/macros/#type-macro-inline-controls"


def main() -> None:
    argv = sys.argv[1:]

    import argparse

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
        help="Type text and inline controls",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Example: keymasq type "user<tab><wait:100:250>password<enter>"\n'
            'Example: keymasq type "<move:420:180><click>"\n'
            f"Type inline controls: {_type_controls_docs_url()}\n"
            f"Full reference: {_docs_url()}"
        ),
    )
    type_parser.add_argument(
        "--wait", action="store_true", help="Wait for completion; interrupt to cancel this playback"
    )
    type_parser.add_argument(
        "--ordered", action="store_true", help="Serialize with other requests that use --ordered"
    )
    _add_json_output(type_parser)
    type_parser.add_argument("text", nargs="*", help="Text to type; stdin is used when omitted")
    type_parser.add_argument(
        "--down-ms",
        type=int,
        default=DEFAULT_TYPE_MACRO_DOWN_MS,
        help="Key down duration",
    )
    type_parser.add_argument(
        "--pause-ms",
        type=int,
        default=DEFAULT_TYPE_MACRO_PAUSE_MS,
        help="Pause between characters",
    )
    type_parser.add_argument(
        "--no-unicode",
        action="store_true",
        help="Fail on unsupported characters instead of using Linux Ctrl+Shift+U input",
    )
    type_parser.add_argument(
        "--layout",
        default=None,
        help="Keyboard layout to type for (default: the Keymasq keyboard layout setting)",
    )
    type_parser.add_argument("--speed", type=_positive_float, default=1.0, help="Playback speed")
    type_parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the compiled macro JSON instead of playing it",
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
    macros_play_parser.add_argument(
        "--wait", action="store_true", help="Wait for completion; interrupt to cancel this playback"
    )
    macros_play_parser.add_argument(
        "--ordered", action="store_true", help="Serialize with other requests that use --ordered"
    )
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

    mpris_parser = subparsers.add_parser(
        "mpris",
        help="MPRIS media player controls",
        epilog=f"Full reference: {_docs_url()}",
    )
    mpris_sub = mpris_parser.add_subparsers(dest="mpris_command", required=True)
    for command, help_text in (
        ("play-pause", "Pause all playing players or resume the latest started player"),
        ("play", "Play the latest started player"),
        ("pause", "Pause all playing players"),
        ("next", "Skip on the latest capable player"),
        ("previous", "Go back on the latest capable player"),
        ("stop", "Stop all playing players"),
    ):
        command_parser = mpris_sub.add_parser(command, help=help_text)
        _add_json_output(command_parser)
    mpris_status_parser = mpris_sub.add_parser("status", help="Show tracked MPRIS players")
    _add_json_output(mpris_status_parser)

    diagnostics_parser = subparsers.add_parser("diagnostics", help="Toggle keymasqd diagnostics")
    diagnostics_parser.add_argument("state", choices=["on", "off"], help="Enable or disable")
    diagnostics_parser.add_argument(
        "--interval",
        type=_positive_float,
        default=5.0,
        help="Logging interval in seconds when enabled",
    )
    diagnostics_parser.add_argument(
        "--include",
        action="append",
        choices=("mainline", "combo", "macro", "internal", "all"),
        default=[],
        help="Diagnostics category to log; repeat to add categories",
    )
    diagnostics_parser.add_argument(
        "--exclude",
        action="append",
        choices=("mainline", "combo", "macro", "internal"),
        default=[],
        help="Diagnostics category to hide after includes are applied",
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

    masking_parser = subparsers.add_parser(
        "masking",
        help="Hardware masking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "DEVICE is a device ID or unique ID prefix, a connection such as 3-2.1, "
            "VENDOR:PRODUCT, or a device name.\n"
            f"Full reference: {_docs_url()}"
        ),
    )
    masking_sub = masking_parser.add_subparsers(dest="masking_command", required=True)

    masking_list_parser = masking_sub.add_parser("list", help="List devices and their masks")
    _add_json_output(masking_list_parser, "Print masking state as JSON")

    masking_show_parser = masking_sub.add_parser("show", help="Show masking details for a device")
    masking_show_parser.add_argument("device", metavar="DEVICE", help="Device to show")
    _add_json_output(masking_show_parser, "Print masking state as JSON")

    masking_enable_parser = masking_sub.add_parser("enable", help="Mask a device")
    masking_enable_parser.add_argument("device", metavar="DEVICE", help="Device to mask")
    masking_enable_confirmation = masking_enable_parser.add_mutually_exclusive_group()
    masking_enable_confirmation.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Keep the mask without asking whether input still works",
    )
    masking_enable_confirmation.add_argument(
        "--no-prompt",
        action="store_true",
        help="Do not ask; keep the mask with 'keymasq masking confirm' before the deadline",
    )
    _add_json_output(masking_enable_parser, "Print masking state as JSON")

    masking_confirm_parser = masking_sub.add_parser(
        "confirm", help="Keep a mask that is waiting for confirmation"
    )
    masking_confirm_parser.add_argument("device", metavar="DEVICE", help="Device to confirm")
    _add_json_output(masking_confirm_parser, "Print masking state as JSON")

    masking_disable_parser = masking_sub.add_parser(
        "disable", help="Turn masking off and forget the saved choice"
    )
    masking_disable_target = masking_disable_parser.add_mutually_exclusive_group(required=True)
    masking_disable_target.add_argument(
        "device", nargs="?", metavar="DEVICE", help="Device to unmask"
    )
    masking_disable_target.add_argument("--all", action="store_true", help="Unmask all devices")
    masking_disable_parser.add_argument(
        "--no-wait", action="store_true", help="Return before device access is restored"
    )
    _add_json_output(masking_disable_parser, "Print masking state as JSON")

    masking_resume_parser = masking_sub.add_parser(
        "resume", help="Enable remapping after an administrative recovery"
    )
    _add_json_output(masking_resume_parser, "Print masking state as JSON")

    args = parser.parse_args(argv)
    json_output = bool(getattr(args, "global_json", False)) or bool(
        getattr(args, "json_output", False)
    )
    if args.command is None:
        parser.print_help()
        return

    from keymasq.cli import commands

    if args.command == "status":
        commands.status_cli(json_output=json_output)
    elif args.command == "type":
        commands.type_cli(
            args.text,
            down_ms=args.down_ms,
            pause_ms=args.pause_ms,
            speed=args.speed,
            use_unicode_input=not args.no_unicode,
            layout=args.layout,
            print_json=args.print_json,
            wait=args.wait,
            ordered=args.ordered,
            json_output=json_output,
        )
    elif args.command == "macros":
        if args.macros_command == "list":
            commands.list_macros_cli(json_output=json_output)
        elif args.macros_command == "create":
            commands.create_macro_cli(
                args.name,
                args.json,
                force=args.force,
                json_output=json_output,
            )
        elif args.macros_command == "play":
            commands.play_macro_cli(
                args.name,
                args.speed,
                json_output=json_output,
                wait=args.wait,
                ordered=args.ordered,
            )
        elif args.macros_command == "cancel":
            commands.cancel_macro_cli(json_output=json_output)
        elif args.macros_command == "delete":
            commands.delete_macro_cli(args.name, json_output=json_output)
    elif args.command == "mpris":
        if args.mpris_command == "status":
            commands.mpris_status_cli(json_output=json_output)
        else:
            commands.mpris_cli(args.mpris_command, json_output=json_output)
    elif args.command == "diagnostics":
        commands.set_diagnostics_cli(
            args.state == "on",
            args.interval,
            include=args.include,
            exclude=args.exclude,
            json_output=json_output,
        )
    elif args.command == "masking":
        from keymasq.cli import masking

        if args.masking_command == "list":
            masking.list_cli(json_output=json_output)
        elif args.masking_command == "show":
            masking.show_cli(args.device, json_output=json_output)
        elif args.masking_command == "enable":
            masking.enable_cli(
                args.device,
                confirmation="yes" if args.yes else "later" if args.no_prompt else "ask",
                json_output=json_output,
            )
        elif args.masking_command == "confirm":
            masking.confirm_cli(args.device, json_output=json_output)
        elif args.masking_command == "disable":
            masking.disable_cli(
                None if args.all else args.device,
                wait=not args.no_wait,
                json_output=json_output,
            )
        elif args.masking_command == "resume":
            masking.resume_cli(json_output=json_output)
    elif args.command == "profiles":
        if args.profiles_command == "list":
            commands.list_profiles_cli(json_output=json_output)
        elif args.profiles_command == "enable":
            commands.set_profile_state_cli(
                "enable_profile",
                args.profile_name,
                json_output=json_output,
            )
        elif args.profiles_command == "disable":
            commands.set_profile_state_cli(
                "disable_profile",
                args.profile_name,
                json_output=json_output,
            )
        elif args.profiles_command == "toggle":
            commands.set_profile_state_cli(
                "toggle_profile",
                args.profile_name,
                json_output=json_output,
            )


if __name__ == "__main__":
    main()
