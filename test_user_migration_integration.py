import os

import pytest

from migrate_users_to_db import build_migration_plan, load_route_references, load_user_rows, verify_plan


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1"
    or not os.getenv("AIVEN_DATABASE_URL")
    or not os.getenv("SHUTTLE_TOKEN_ENCRYPTION_KEY"),
    reason="Set integration flag, database URL, and token encryption key for Aiven tests.",
)


def test_aiven_user_migration_matches_json_without_push_rows():
    plan = build_migration_plan(load_user_rows(), load_route_references())
    report = verify_plan(plan)
    assert report["users"] == len(plan.users)
    assert report["credentials"] == len(plan.users)
    assert report["favorites"] == len(plan.favorites)
    assert report["favorite_notifications"] == len(plan.favorites)
    assert report["token_decrypt_match"] is True
    assert report["push_subscriptions"] == 0
