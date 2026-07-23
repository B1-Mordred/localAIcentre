CREATE TABLE IF NOT EXISTS b1_bootstrap_marker (
    id integer PRIMARY KEY DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (id = 1)
);

INSERT INTO b1_bootstrap_marker (id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;
