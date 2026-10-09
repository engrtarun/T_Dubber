"""Validate /api/status against the exact fields dashboard/index.html reads.

Not a generic schema check: this mirrors the render() contract so a contract
drift between dashboard_server.py and index.html fails loudly here.
"""
import json
import sys
import urllib.request

# 8081, not 8080: havaldar_core owns 8080. Pointing this at 8080 means
# silently contract-testing the telemetry daemon instead of this server.
BASE = "http://127.0.0.1:8081"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


fails = []


def need(cond, msg):
    if not cond:
        fails.append(msg)


# The checks below used to run at import time. `pytest` passes the file name
# as argv[1], so collecting this file tried to GET
# "dashboard_contract_test.py/api/status" and died with "unknown url type"
# -- a bare `pytest` at the repo root could never even start. Importing the
# module also hit the network, which is its own kind of surprise. This is a
# script against a LIVE server, not a unit test, so it runs behind __main__:


def main(base=BASE):
    """Poll a running dashboard server and check the render() contract."""
    global BASE
    BASE = base
    fails = []
    # ---------------------------------------------------------------- /api/status
    status, snap = get("/api/status")

    for key in ("project", "progress", "engines", "metrics", "stages", "journal", "state", "log"):
        need(key in snap, f"snapshot missing top-level '{key}'")

    for key in ("id", "title", "kernel_id", "target_language", "created_at", "output_video", "model"):
        need(key in snap["project"], f"snapshot.project missing '{key}'")

    for key in ("chunk_index", "chunk_count", "pct", "phase", "chunk_error"):
        need(key in snap["progress"], f"snapshot.progress missing '{key}'")

    ENG = {"python", "ffmpeg", "rust", "go"}
    need(set(snap["engines"]) == ENG, f"engines must be exactly {ENG}, got {set(snap['engines'])}")

    VALID_ENG = {"idle", "running", "done", "queued", "error"}
    for name, e in snap["engines"].items():
        for key in ("state", "detail", "pct"):
            need(key in e, f"engines.{name} missing '{key}'")
        need(e.get("state") in VALID_ENG, f"engines.{name}.state invalid: {e.get('state')!r}")
        need(0 <= (e.get("pct") or 0) <= 100, f"engines.{name}.pct out of range: {e.get('pct')}")

    for key in ("elapsed_sec", "eta_sec", "uplink_mbps", "vram_gb", "rtf", "parts_stored", "parts_total"):
        need(key in snap["metrics"], f"snapshot.metrics missing '{key}'")

    VALID_STG = {"pending", "running", "success", "done", "failed", "skipped"}
    seen_idx = set()
    for s in snap["stages"]:
        for key in ("stage", "status", "ms"):
            need(key in s, f"stage row missing '{key}'")
        need(s["status"] in VALID_STG, f"stage status invalid: {s['status']!r}")
        need(0 <= s["stage"] <= 8, f"stage index out of 0..8: {s['stage']}")
        seen_idx.add(s["stage"])
    need(seen_idx, "stages is empty - the stage rail would render dead")
    need(len(snap["stages"]) == 9, f"expected 9 stage rows, got {len(snap['stages'])}")
    need(len(seen_idx) == len(snap["stages"]), "duplicate stage indices")

    need(set(snap["journal"]) >= {"channel", "source_sha256"}, "journal keys missing")

    # log contract: entries are objects carrying a stable id + level + msg
    VALID_LOG = {"info", "ok", "warn", "err"}
    for entry in snap["log"]:
        need(isinstance(entry, dict), f"log entry is not an object: {entry!r}")
        need(entry.get("id") not in (None, ""), f"log entry missing id: {entry!r}")
        need(entry.get("level") in VALID_LOG, f"log level invalid: {entry.get('level')!r}")
        need(isinstance(entry.get("msg"), str), f"log msg not a string: {entry!r}")

    # numeric hygiene: nothing that would print NaN in the UI
    blob = json.dumps(snap)
    need("NaN" not in blob, "payload contains NaN (renders literally in the UI)")
    need("Infinity" not in blob, "payload contains Infinity")


    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
        elif isinstance(o, float):
            yield o


    bad = [v for v in walk(snap) if v != v or v in (float("inf"), float("-inf"))]
    need(not bad, f"non-finite floats in payload: {bad[:3]}")

    # ------------------------------------------------- log id stability (3 polls)
    id_runs = []
    for _ in range(3):
        _, s = get("/api/status")
        id_runs.append([e["id"] for e in s["log"]])
    need(id_runs[0] == id_runs[1] == id_runs[2],
         "log ids are not stable across polls - the UI would re-log or drop lines")
    need(len(set(id_runs[0])) == len(id_runs[0]), "duplicate ids within one payload")

    # ------------------------------------------------- other endpoints
    _, projs = get("/api/projects")
    need(isinstance(projs.get("projects"), list) and projs["projects"],
         "/api/projects returned no rows (db has 67)")

    _, q = get("/api/quota")
    need("channels" in q and "day" in q, "/api/quota shape wrong")
    for ch in q["channels"]:
        need("used_mb" in ch and "source" in ch, f"quota channel missing keys: {ch}")
        need(ch["source"] in ("channel_daily_quota", "observed:telegram_archives"),
             f"unexpected quota source: {ch['source']!r}")

    print(f"snapshot keys      : {sorted(snap)}")
    print(f"state              : {snap['state']}")
    print(f"stages             : {len(snap['stages'])} rows, indices {sorted(seen_idx)}")
    print(f"engines            : " + ", ".join(f"{k}={v['state']}" for k, v in snap["engines"].items()))
    print(f"quota channels     : {[(c['channel'], c['used_mb'], c['source']) for c in q['channels']]}")
    print(f"log ids stable     : {len(set(id_runs[0]))} unique across 3 polls")
    print(f"projects listed    : {len(projs['projects'])}")
    print()
    if fails:
        print("RESULT: FAIL")
        for f in fails:
            print("  !", f)
        return 1
    print("RESULT: PASS - payload satisfies the render() contract")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081"))
