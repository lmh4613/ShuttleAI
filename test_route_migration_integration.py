import os

import pytest

from migrate_routes_to_db import build_migration_plan, load_route_rows, verify_plan


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1" or not os.getenv("AIVEN_DATABASE_URL"),
    reason="Set RUN_AIVEN_INTEGRATION_TESTS=1 with AIVEN_DATABASE_URL for Aiven tests.",
)


def test_aiven_route_migration_matches_json_and_defaults():
    plan = build_migration_plan(load_route_rows())
    report = verify_plan(plan)
    assert report == {
        "routes": len(plan.routes),
        "stops": len(plan.stops),
        "route_stops": len(plan.route_stops),
        "dropoff_only": plan.dropoff_only_rows,
        "default_dropoffs": plan.morning_routes,
    }
