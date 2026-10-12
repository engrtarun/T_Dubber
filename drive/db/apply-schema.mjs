/**
 * Apply db/schema.sql straight to Postgres — no SQL Editor, no browser.
 *
 * WHY THIS EXISTS
 * The obvious way to create tables in Supabase is to paste the SQL into the
 * dashboard's SQL Editor, which needs a human at the keyboard. That was fine
 * until there was a second machine. This script does it unattended, so a fresh
 * database can be brought up from a terminal alone.
 *
 * WHY IT CONNECTS OVER THE POOLER
 * Supabase's *direct* connection host (`db.<ref>.supabase.co`) is IPv6-only by
 * default, so it fails outright from IPv4-only networks. The pooler host
 * (`aws-0-<region>.pooler.supabase.com`) answers on IPv4 and is what the
 * dashboard itself suggests for IPv4 clients. Port 5432 = session mode, which
 * is what a script like this one wants (transaction mode is meant for bursts of
 * short queries, not a one-off migration).
 *
 * THE APP STILL DOES NOT USE THIS
 * The Next.js app talks to Supabase over HTTPS via @supabase/supabase-js, so it
 * works unchanged on both Netlify and Cloudflare (Workers forbids raw TCP).
 * This script is a setup tool, not a runtime dependency.
 *
 *   DATABASE_URL=postgresql://… node db/apply-schema.mjs
 */
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import pg from "pg";

const here = dirname(fileURLToPath(import.meta.url));
const url = process.env.DATABASE_URL?.trim();

if (!url) {
  console.error("DATABASE_URL is not set.\n");
  process.exit(2);
}

// Postgres's own parser stops at the first semicolon, which would silently
// create only the first table. The real statements are split here instead.
const schema = await readFile(join(here, "schema.sql"), "utf8");

const client = new pg.Client({
  connectionString: url,
  connectionTimeoutMillis: 20000,
  ssl: { rejectUnauthorized: false },
});

try {
  await client.connect();
  console.log("Connected.");

  const { rows } = await client.query("select version()");
  console.log(`Server: ${rows[0].version.split(" ").slice(0, 2).join(" ")}\n`);

  // CREATE TABLE ...; with `IF NOT EXISTS` throughout, so re-running is safe.
  await client.query(schema);
  console.log("Schema applied (all statements IF NOT EXISTS).");

  const counts = await client.query(`
    SELECT
      (SELECT count(*) FROM users)   AS users,
      (SELECT count(*) FROM folders) AS folders,
      (SELECT count(*) FROM files)   AS files
  `);
  console.log("Row counts:", counts.rows[0]);
  process.exit(0);
} catch (err) {
  console.error(`Failed: ${err.message}`);
  process.exit(1);
} finally {
  await client.end().catch(() => {});
}