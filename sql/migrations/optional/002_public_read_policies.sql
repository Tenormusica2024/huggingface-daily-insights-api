-- Optional migration: enable public read-only access via Supabase anon key.
-- Run this only if you want to expose the database for direct SQL queries / SDK reads
-- without going through the FastAPI layer. The API itself works without this.
--
-- After applying, clients can use the anon key to SELECT from these tables.
-- Writes remain restricted to the service_role key used by the crawlers.

-- Enable RLS
ALTER TABLE models ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE papers ENABLE ROW LEVEL SECURITY;
ALTER TABLE arena_rankings ENABLE ROW LEVEL SECURITY;

-- Public read policies (allow SELECT for anon and authenticated roles)
CREATE POLICY "public read models"
  ON models FOR SELECT USING (true);

CREATE POLICY "public read model_snapshots"
  ON model_snapshots FOR SELECT USING (true);

CREATE POLICY "public read papers"
  ON papers FOR SELECT USING (true);

CREATE POLICY "public read arena_rankings"
  ON arena_rankings FOR SELECT USING (true);

-- No INSERT/UPDATE/DELETE policies are defined, so writes remain blocked for anon/authenticated.
-- The service_role key bypasses RLS entirely (used by crawlers in GitHub Actions).
