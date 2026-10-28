-- Show settings (server/settings.py defines every key, its range and default).
CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per browser: `id` is the random id the guest page keeps in
-- localStorage + a cookie, so a phone that reconnects is the same client.
CREATE TABLE clients (
    id            TEXT PRIMARY KEY,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    visits        INTEGER NOT NULL DEFAULT 0,
    total_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    latency_sum   DOUBLE PRECISION NOT NULL DEFAULT 0,   -- ms, over every pong
    latency_count INTEGER NOT NULL DEFAULT 0,
    last_ip       TEXT,
    last_fixture  INTEGER,
    user_agent    TEXT,
    device        TEXT,
    os            TEXT,
    browser       TEXT,
    screen        TEXT,
    language      TEXT,
    timezone      TEXT,
    details       TEXT                                   -- JSON: the rest of the hello
);
CREATE INDEX clients_last_seen ON clients (last_seen);

-- One row per WebSocket connection.
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES clients (id) ON DELETE CASCADE,
    ip              TEXT,
    fixture         INTEGER,
    connected_at    TEXT NOT NULL,
    disconnected_at TEXT,                                -- NULL while live
    seconds         DOUBLE PRECISION NOT NULL DEFAULT 0,
    latency_avg     DOUBLE PRECISION,
    latency_min     DOUBLE PRECISION,
    latency_max     DOUBLE PRECISION,
    latency_samples INTEGER NOT NULL DEFAULT 0,
    close_reason    TEXT
);
CREATE INDEX sessions_client ON sessions (client_id, connected_at);
CREATE INDEX sessions_connected_at ON sessions (connected_at);
