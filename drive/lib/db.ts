/**
 * One database, two ways in, two platforms.
 *
 * ── Why Postgres and not SQLite ───────────────────────────────────────────
 * The project's other database (`t_dubber.db` on the `main` branch) is SQLite
 * and that is correct there: a single-writer local telemetry store. This app is
 * different — it runs on Netlify and Cloudflare, where the filesystem is
 * EPHEMERAL. Every cold start and every deploy wipes local disk, so a SQLite
 * file would silently lose everything. Only the network survives both.
 *
 * ── Two backends, and why ─────────────────────────────────────────────────
 *
 *   1. `@supabase/supabase-js` over HTTPS — the PORTABLE path.
 *      Plain HTTP, no sockets, so it runs identically on Netlify Functions and
 *      Cloudflare Workers. Needs `NEXT_PUBLIC_SUPABASE_URL` +
 *      `SUPABASE_SERVICE_ROLE_KEY`.
 *
 *   2. `pg` over raw TCP — the DIRECT path.
 *      Works on Netlify (Node runtime permits sockets) and on anything with a
 *      normal network, using only `DATABASE_URL`. It does NOT work on
 *      Cloudflare Workers' free tier, which forbids outbound TCP.
 *
 * The order below is deliberate: HTTPS first, TCP second. If both are set, the
 * portable one wins, so a Cloudflare deploy never silently depends on a socket
 * it is not allowed to open.
 *
 * ── A correction worth recording ──────────────────────────────────────────
 * `@neondatabase/serverless` was the first choice here, on the reasoning that
 * it is edge-native. Wrong for Supabase: its own source POSTs to
 * `https://<host>/sql` with `Neon-Connection-String` / `Neon-Raw-Text-Output`
 * headers. That is Neon's proprietary gateway; Supabase does not implement it.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import pg from "pg";

// `pg` hands every row as a string; TEXT and DATE belong back as strings, but
// numbers and booleans should not silently arrive as "0" and "false".
pg.types.setTypeParser(20, (v) => Number(v)); // int8
pg.types.setTypeParser(1700, (v) => Number(v)); // numeric

function env(name: string): string | undefined {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : undefined;
}

export type Backend = "supabase" | "postgres" | null;

/** Which path is available, preferring the portable one. */
export function backend(): Backend {
  if (env("NEXT_PUBLIC_SUPABASE_URL") && env("SUPABASE_SERVICE_ROLE_KEY")) {
    return "supabase";
  }
  if (env("DATABASE_URL")) return "postgres";
  return null;
}

/** True when some path is configured. Used by /api/db-check. */
export function isConfigured(): boolean {
  return backend() !== null;
}

function missing(): string[] {
  return [
    !env("NEXT_PUBLIC_SUPABASE_URL") && "NEXT_PUBLIC_SUPABASE_URL",
    !env("SUPABASE_SERVICE_ROLE_KEY") && "SUPABASE_SERVICE_ROLE_KEY",
    !env("DATABASE_URL") && "DATABASE_URL",
  ].filter(Boolean) as string[];
}

export const SETUP_HINT =
  "Set NEXT_PUBLIC_SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (Supabase → " +
  "Project Settings → API), or DATABASE_URL for the direct TCP path.";

// ── Supabase (HTTPS) ───────────────────────────────────────────────────────

let client: SupabaseClient | null = null;

export function supabase(): SupabaseClient {
  if (client) return client;
  const url = env("NEXT_PUBLIC_SUPABASE_URL");
  const key = env("SUPABASE_SERVICE_ROLE_KEY");
  if (!url || !key) {
    throw new Error(`Missing: ${missing().join(", ")}. ${SETUP_HINT}`);
  }
  client = createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  return client;
}

// ── Postgres (TCP) ──────────────────────────────────────────────────────────

let pool: pg.Pool | null = null;

export function postgres(): pg.Pool {
  if (pool) return pool;
  const url = env("DATABASE_URL");
  if (!url) {
    throw new Error(`Missing: DATABASE_URL. ${SETUP_HINT}`);
  }
  pool = new pg.Pool({
    connectionString: url,
    max: 2, // serverless: one instance may be handling a burst of concurrent requests
    connectionTimeoutMillis: 15000,
    ssl: { rejectUnauthorized: false }, // Supabase uses a managed cert chain
    idleTimeoutMillis: 10000,
  });
  return pool;
}