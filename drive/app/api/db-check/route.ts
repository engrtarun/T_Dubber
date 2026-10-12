import { NextResponse } from "next/server";
import {
  backend,
  isConfigured,
  postgres,
  SETUP_HINT,
  supabase,
} from "@/lib/db";

/**
 * Proves the database is reachable FROM THE DEPLOYED PLATFORM.
 *
 * "Works on my machine" is exactly the claim that breaks on serverless: the
 * failure mode of missing credentials is an app that renders fine and stores
 * nothing. This route cannot lie — it either returns row counts read back out
 * of the database, or the exact error that stopped it.
 *
 *   GET /api/db-check
 */
export const dynamic = "force-dynamic";

const TABLES = ["users", "folders", "files"] as const;

async function viaSupabase() {
  const db = supabase();
  // head:true asks for the count only — no rows cross the wire.
  const pairs = await Promise.all(
    TABLES.map(async (table) => {
      const { count, error } = await db
        .from(table)
        .select("*", { count: "exact", head: true });
      return [table, error ? { error: error.message } : { rows: count ?? 0 }] as const;
    }),
  );
  return { counts: Object.fromEntries(pairs), pairs };
}

async function viaPostgres() {
  const pool = postgres();
  const counts: Record<string, { rows: number } | { error: string }> = {};
  for (const table of TABLES) {
    try {
      const { rows } = await pool.query(
        `SELECT count(*)::int AS n FROM ${table}`,
      );
      counts[table] = { rows: rows[0].n };
    } catch (err) {
      counts[table] = { error: err instanceof Error ? err.message : String(err) };
    }
  }
  return counts;
}

export async function GET() {
  const via = backend();

  if (!via) {
    return NextResponse.json(
      {
        ok: false,
        configured: false,
        error:
          "No database credentials on this deployment. " + SETUP_HINT +
          " (Netlify: site environment variables · Cloudflare: Pages environment variables).",
      },
      { status: 503 },
    );
  }

  const started = Date.now();

  try {
    const counts =
      via === "supabase"
        ? (await viaSupabase()).counts
        : await viaPostgres();

    const broken = Object.entries(counts).filter(([, v]) => "error" in v);

    if (broken.length) {
      return NextResponse.json(
        {
          ok: false,
          configured: true,
          backend: via,
          error: broken
            .map(([table, v]) => `${table}: ${(v as { error: string }).error}`)
            .join(" · "),
          hint:
            "Reachable, but the drive tables are not there. Apply the schema: " +
            "npm run db:schema  (or paste db/schema.sql into the Supabase SQL Editor).",
          counts,
          latency_ms: Date.now() - started,
        },
        { status: 502 },
      );
    }

    return NextResponse.json({
      ok: true,
      configured: true,
      backend: via,
      counts,
      latency_ms: Date.now() - started,
    });
  } catch (err) {
    return NextResponse.json(
      {
        ok: false,
        configured: true,
        backend: via,
        error: err instanceof Error ? err.message : String(err),
      },
      { status: 502 },
    );
  }
}