-- Devices refused at connect (server/blocks.py): by the guest page's client id or by IP.
CREATE TABLE blocks (
    kind       TEXT NOT NULL,                            -- 'client' | 'ip'
    value      TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
