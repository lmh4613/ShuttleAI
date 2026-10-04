ALTER TABLE notification_settings
    ADD COLUMN IF NOT EXISTS delivery_channel TEXT NOT NULL DEFAULT 'KAKAO'
    CHECK (delivery_channel IN ('PUSH', 'KAKAO'));

CREATE TABLE IF NOT EXISTS service_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    notification_channel_policy TEXT NOT NULL DEFAULT 'AUTO'
        CHECK (notification_channel_policy IN ('AUTO', 'PUSH', 'KAKAO')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_service_settings_updated_at ON service_settings;
CREATE TRIGGER trg_service_settings_updated_at BEFORE UPDATE ON service_settings
FOR EACH ROW EXECUTE FUNCTION shuttleai_set_updated_at();

INSERT INTO service_settings (singleton, notification_channel_policy)
VALUES (TRUE, 'AUTO')
ON CONFLICT (singleton) DO NOTHING;
