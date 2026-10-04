ALTER TABLE routes
    ADD COLUMN IF NOT EXISTS inactive_reason TEXT
    CHECK (inactive_reason IS NULL OR inactive_reason = 'ROUTE_REMOVED');

ALTER TABLE route_stops
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE route_stops
    ADD COLUMN IF NOT EXISTS inactive_reason TEXT
    CHECK (inactive_reason IS NULL OR inactive_reason IN ('ROUTE_REMOVED', 'STOP_REMOVED'));

ALTER TABLE favorites
    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE favorites
    ADD COLUMN IF NOT EXISTS inactive_reason TEXT
    CHECK (inactive_reason IS NULL OR inactive_reason IN ('ROUTE_REMOVED', 'STOP_REMOVED'));

ALTER TABLE route_stops
    DROP CONSTRAINT IF EXISTS uq_route_stops_route_order;

CREATE UNIQUE INDEX IF NOT EXISTS uq_route_stops_route_order_active
    ON route_stops (route_id, stop_order)
    WHERE active;

CREATE INDEX IF NOT EXISTS idx_favorites_active_user
    ON favorites (user_id, id)
    WHERE active;
