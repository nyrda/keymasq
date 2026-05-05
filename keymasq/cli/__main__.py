from __future__ import annotations

import sys

from keymasq import __version__


def _positive_float(value: str) -> float:
    import argparse

    return _parse_positive_float(value, argparse.ArgumentTypeError)


def _parse_positive_float(value: str, error_type: type[Exception] = ValueError) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise error_type("must be greater than 0")
    return parsed


def _add_json_output(parser) -> None:
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
    argv = sys.argv[1:]
    if _try_fast_dispatch(argv):
        return

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


def _try_fast_dispatch(argv: list[str]) -> bool:
    if not argv or any(arg in {"-h", "--help"} for arg in argv):
        return False

    if argv == ["--version"]:
        print(f"keymasq {__version__}")
        sys.exit(0)

    json_output = False
    if argv and argv[0] == "--json":
        json_output = True
        argv = argv[1:]
    if not argv:
        return False

    command = argv[0]
    rest = argv[1:]
    if command == "status":
        if rest == ["--json"]:
            json_output = True
            rest = []
        if rest:
            return False

        status_cli(json_output=json_output)
        return True

    if command == "type":
        return _try_fast_type(rest, json_output=json_output)

    if command == "play":
        return _try_fast_play(rest, json_output=json_output)

    return False


def _try_fast_type(argv: list[str], *, json_output: bool) -> bool:
    text: list[str] = []
    down_ms = 10
    pause_ms = 20
    speed = 1.0
    use_unicode_input = True
    print_json = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            text.extend(argv[index + 1 :])
            break
        if arg == "--no-unicode":
            use_unicode_input = False
            index += 1
            continue
        if arg == "--print-json":
            print_json = True
            index += 1
            continue
        if arg in {"--down-ms", "--pause-ms", "--speed"}:
            if index + 1 >= len(argv):
                return False
            value = argv[index + 1]
            try:
                if arg == "--down-ms":
                    down_ms = int(value)
                elif arg == "--pause-ms":
                    pause_ms = int(value)
                else:
                    speed = _parse_positive_float(value)
            except ValueError:
                return False
            index += 2
            continue
        if arg.startswith("-"):
            return False
        text.append(arg)
        index += 1

    type_cli(
        text,
        down_ms=down_ms,
        pause_ms=pause_ms,
        speed=speed,
        use_unicode_input=use_unicode_input,
        print_json=print_json,
        json_output=json_output,
    )
    return True


def _try_fast_play(argv: list[str], *, json_output: bool) -> bool:
    tokens: list[str] = []
    input_json = False
    speed = 1.0
    print_json = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            tokens.extend(argv[index + 1 :])
            break
        if arg == "--json":
            input_json = True
            index += 1
            continue
        if arg == "--print-json":
            print_json = True
            index += 1
            continue
        if arg == "--speed":
            if index + 1 >= len(argv):
                return False
            try:
                speed = _parse_positive_float(argv[index + 1])
            except ValueError:
                return False
            index += 2
            continue
        if arg.startswith("-"):
            return False
        tokens.append(arg)
        index += 1

    play_adhoc_cli(
        tokens,
        input_json=input_json,
        speed=speed,
        print_json=print_json,
        json_output=json_output,
    )
    return True


def status_cli(*, json_output: bool = False) -> None:
    from keymasq.cli.commands import status_cli as impl

    impl(json_output=json_output)


def type_cli(
    text: list[str],
    *,
    down_ms: int = 10,
    pause_ms: int = 20,
    speed: float = 1.0,
    use_unicode_input: bool = True,
    print_json: bool = False,
    json_output: bool = False,
) -> None:
    from keymasq.cli.commands import type_cli as impl

    impl(
        text,
        down_ms=down_ms,
        pause_ms=pause_ms,
        speed=speed,
        use_unicode_input=use_unicode_input,
        print_json=print_json,
        json_output=json_output,
    )


def play_adhoc_cli(
    events: list[str],
    *,
    input_json: bool = False,
    speed: float = 1.0,
    print_json: bool = False,
    json_output: bool = False,
) -> None:
    from keymasq.cli.commands import play_adhoc_cli as impl

    impl(
        events,
        input_json=input_json,
        speed=speed,
        print_json=print_json,
        json_output=json_output,
    )


def list_macros_cli(*, json_output: bool = False) -> None:
    from keymasq.cli.commands import list_macros_cli as impl

    impl(json_output=json_output)


def create_macro_cli(
    name: str,
    json_parts: list[str],
    *,
    force: bool = False,
    json_output: bool = False,
) -> None:
    from keymasq.cli.commands import create_macro_cli as impl

    impl(name, json_parts, force=force, json_output=json_output)


def play_macro_cli(name: str, speed: float = 1.0, *, json_output: bool = False) -> None:
    from keymasq.cli.commands import play_macro_cli as impl

    impl(name, speed, json_output=json_output)


def cancel_macro_cli(*, json_output: bool = False) -> None:
    from keymasq.cli.commands import cancel_macro_cli as impl

    impl(json_output=json_output)


def delete_macro_cli(name: str, *, json_output: bool = False) -> None:
    from keymasq.cli.commands import delete_macro_cli as impl

    impl(name, json_output=json_output)


def set_diagnostics_cli(
    enabled: bool,
    interval: float = 5.0,
    *,
    json_output: bool = False,
) -> None:
    from keymasq.cli.commands import set_diagnostics_cli as impl

    impl(enabled, interval, json_output=json_output)


def list_profiles_cli(*, json_output: bool = False) -> None:
    from keymasq.cli.commands import list_profiles_cli as impl

    impl(json_output=json_output)


def set_profile_state_cli(command: str, profile_name: str, *, json_output: bool = False) -> None:
    from keymasq.cli.commands import set_profile_state_cli as impl

    impl(command, profile_name, json_output=json_output)


if __name__ == "__main__":
    main()
