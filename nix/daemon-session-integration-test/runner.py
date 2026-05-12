#!/usr/bin/env python3

from scenarios import SCENARIOS
from support import ScenarioContext


def main() -> int:
    context = ScenarioContext()
    try:
        context.setup()
        for scenario in SCENARIOS:
            context.subtest(scenario.name, lambda run=scenario.run: run(context))
    finally:
        context.cleanup()

    print("integration: daemon/session smoke passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
