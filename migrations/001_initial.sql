CREATE EXTENSION IF NOT EXISTS vector;

-- Auto-update updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS memories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(8192),
    metadata    JSONB DEFAULT '{}'::jsonb,
    namespace   TEXT NOT NULL DEFAULT 'default',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories (user_id);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories (namespace);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories (created_at DESC);

-- Vector similarity index (IVFFlat, cosine distance)
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON memories
    USING ivf (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Trigger to auto-update updated_at on row modification
CREATE TRIGGER trg_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- DOWN migration (for --down flag)
-- DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
-- DROP TABLE IF EXISTS memories CASCADE;
-- DROP FUNCTION IF EXISTS update_updated_at_column();
