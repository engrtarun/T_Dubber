/**
 * Check that the drive schema exists, from your machine.
 *
 *   npm run db:check
 *
 * Tries both backends in the same order the app uses (HTTPS first, TCP second)
 * and reports which one answered. It creates nothing — `npm run db:schema` is
 * the command that applies the schema.
 */
import { createClient } from "@supabase/supabase-js";
import pg from "pg";

const TABLES = ["users", "folders", "files"];
const v = (n) => process.env[n]?.trim();

function pickBackend() {
  if (v("NEXT_PUBLIC_SUPABASE_URL") && v("SUPABASE_SERVICE_ROLE_KEY")) return "supabase";
  if (v("DATABASE_URL")) return "postgres";
  return null;
}

const via = pickBackend();

if (!via) {
  console.error(
    "No database credentials found.\n\n" +
      "  Option A (portable — HTTPS, works on Netlify AND Cloudflare):\n" +
      "    NEXT_PUBLIC_SUPABASE_URL  + SUPABASE_SERVICE_ROLE_KEY\n" +
      "    Supabase → Project Settings → API\n\n" +
      "  Option B (direct — TCP, works on Netlify, NOT Cloudflare free tier):\n" +
      "    DATABASE_URL=postgresql://...\n\n" +
      "Put them in drive/.env.local.\n",
  );
  process.exit(2);
}

console.log(`Backend: ${via}\n`);

let missing = 0;

if (via === "supabase") {
  const db = createClient(v("NEXT_PUBLIC_SUPABASE_URL"), v("SUPABASE_SERVICE_ROLE_KEY"), {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  for (const table of TABLES) {
    const { count, error } = await db.from(table).select("*", { count: "exact", head: true });
    if (error) { missing += 1; console.log(`  MISSING  ${table.padEnd(8)} — ${error.message}`); }
    else console.log(`  ok       ${table.padEnd(8)} — ${count ?? 0} row(s)`);
  }
} else {
  const pool = new pg.Pool({
    connectionString: v("DATABASE_URL"),
    max: 1,
    connectionTimeoutMillis: 20000,
    ssl: { rejectUnauthorized: false },
  });
  try {
    for (const table of TABLES) {
      try {
        const { rows } = await pool.query(`SELECT count(*)::int AS n FROM ${table}`);
        console.log(`  ok       ${table.padEnd(8)} — ${rows[0].n} row(s)`);
      } catch (err) {
        missing += 1;
        console.log(`  MISSING  ${table.padEnd(8)} — ${err.message}`);
      }
    }
  } finally {
    await pool.end().catch(() => {});
  }
}

if (missing) {
  console.error(`\n${missing} table(s) missing. Apply the schema:\n  npm run db:schema\n`);
  process.exit(1);
}
console.log("\nSchema is in place.");
process.exit(0);