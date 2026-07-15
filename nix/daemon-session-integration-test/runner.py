#!/usr/bin/env python3

import argparse
import os
import re
import sys

from scenarios import SCENARIOS
from support import ScenarioCase, ScenarioContext


def _scenario_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _split_scenario_filter(values: list[str]) -> list[str]:
    names: list[str] = []
    for value in values:
        names.extend(name.strip() for name in value.split(",") if name.strip())
    return names


def selected_scenarios(raw_names: list[str]) -> list[ScenarioCase]:
    wanted = _split_scenario_filter(raw_names)
    if not wanted:
        return SCENARIOS

    by_name = {
        key: scenario
        for scenario in SCENARIOS
        for key in (scenario.name, scenario.name.lower(), _scenario_key(scenario.name))
    }
    missing = [
        name
        for name in wanted
        if name not in by_name and _scenario_key(name) not in by_name
    ]
    if missing:
        raise AssertionError(f"unknown integration scenarios: {missing}")
    return [
        by_name[name] if name in by_name else by_name[_scenario_key(name)]
        for name in wanted
    ]


def _env_repeat_count() -> int:
    raw = os.environ.get("KEYMASQ_INTEGRATION_REPEAT", "").strip()
    if not raw:
        return 1
    try:
        repeat = int(raw)
    except ValueError as exc:
        raise AssertionError(f"invalid KEYMASQ_INTEGRATION_REPEAT={raw!r}") from exc
    if repeat < 1:
        raise AssertionError("KEYMASQ_INTEGRATION_REPEAT must be >= 1")
    return repeat


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run daemon/session integration scenarios")
    parser.add_argument(
        "scenarios",
        nargs="*",
        help="Scenario names or kebab-case scenario keys; comma-separated values are accepted.",
    )
    parser.add_argument(
        "--scenario",
        "-s",
        action="append",
        default=[],
        help="Scenario name/key to run. Can be passed multiple times or comma-separated.",
    )
    parser.add_argument(
        "--repeat",
        "-r",
        type=int,
        default=None,
        help="Run the selected scenarios this many times.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every registered scenario key without starting the VM test.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        for scenario in SCENARIOS:
            print(f"{_scenario_key(scenario.name)}\t{scenario.name}")
        return 0
    scenario_filters = list(args.scenario) + list(args.scenarios)
    if not scenario_filters:
        env_filter = os.environ.get("KEYMASQ_INTEGRATION_SCENARIOS", "").strip()
        if env_filter:
            scenario_filters.append(env_filter)
    repeat = args.repeat if args.repeat is not None else _env_repeat_count()
    if repeat < 1:
        raise AssertionError("--repeat must be >= 1")
    scenarios = selected_scenarios(scenario_filters)

    context = ScenarioContext()
    try:
        context.setup()
        for index in range(1, repeat + 1):
            if repeat > 1:
                print(f"integration: repeat {index}/{repeat}", flush=True)
            for scenario in scenarios:
                label = scenario.name if repeat == 1 else f"{scenario.name} [{index}/{repeat}]"
                context.subtest(label, lambda run=scenario.run: run(context))
    finally:
        context.cleanup()

    print("integration: daemon/session smoke passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
