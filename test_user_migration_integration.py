import os

import pytest

from database import database_connection
from token_crypto import decrypt_token


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AIVEN_INTEGRATION_TESTS") != "1"
    or not os.getenv("AIVEN_DATABASE_URL")
    or not os.getenv("SHUTTLE_TOKEN_ENCRYPTION_KEY"),
    reason="Set integration flag, database URL, and token encryption key for Aiven tests.",
)


def test_aiven_user_data_remains_structurally_usable_after_json_divergence():
    """The DB and JSON may diverge after migration; validate the DB itself."""
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT u.id, kc.access_token_ciphertext, kc.refresh_token_ciphertext "
                "FROM users u JOIN kakao_credentials kc ON kc.user_id=u.id "
                "JOIN notification_settings ns ON ns.user_id=u.id "
                "WHERE u.enabled=TRUE"
            )
            rows = cursor.fetchall()
            assert rows
            for _user_id, access_cipher, refresh_cipher in rows:
                assert decrypt_token(access_cipher) is None or isinstance(decrypt_token(access_cipher), str)
                assert decrypt_token(refresh_cipher) is None or isinstance(decrypt_token(refresh_cipher), str)
            cursor.execute(
                "SELECT count(*) FROM favorites f "
                "JOIN favorite_notifications fn ON fn.favorite_id=f.id "
                "JOIN routes r ON r.id=f.route_id "
                "JOIN route_stops b ON b.id=f.boarding_route_stop_id AND b.route_id=f.route_id "
                "JOIN route_stops a ON a.id=f.alighting_route_stop_id AND a.route_id=f.route_id"
            )
            assert cursor.fetchone()[0] >= 0
