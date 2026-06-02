#!/usr/bin/env python3

import os

from scenarios import SCENARIOS
from support import ScenarioCase, ScenarioContext


def _scenario_key(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def selected_scenarios() -> list[ScenarioCase]:
    raw = os.environ.get("KEYMASQ_INTEGRATION_SCENARIOS", "").strip()
    if not raw:
        return SCENARIOS

    wanted = [name.strip() for name in raw.split(",") if name.strip()]
    by_name = {
        key: scenario
        for scenario in SCENARIOS
        for key in (scenario.name, _scenario_key(scenario.name))
    }
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise AssertionError(f"unknown integration scenarios: {missing}")
    return [by_name[name] for name in wanted]


def main() -> int:
    context = ScenarioContext()
    try:
        context.setup()
        for scenario in selected_scenarios():
            context.subtest(scenario.name, lambda run=scenario.run: run(context))
    finally:
        context.cleanup()

    print("integration: daemon/session smoke passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
