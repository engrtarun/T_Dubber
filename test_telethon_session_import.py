"""Carrying the Python login into the go session — "no more OTP" (NIMBU).

$TGUP_SESSION points at a scratch dir, so the real tgup.session — and its
verdict — is never touched, and $TGUP_TELETHON_SESSION points at a fabricated
Telethon file so nothing here depends on the real login sitting on this disk.

Run with:  python -m pytest test_telethon_session_import.py -q
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import telethon_session
import tgup_bridge

ROOT = Path(__file__).resolve().parent

# Deterministic stand-in for a 256-byte MTProto auth key.
KEY = bytes(range(256))


def make_telethon_session(path: Path, *, key: bytes = KEY, dc: int = 5,
                          host: str = "91.108.56.185", port: int = 443) -> Path:
    """A Telethon SQLite session holding exactly what a real login writes."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE sessions ("
            "dc_id integer primary key, server_address text, port integer, "
            "auth_key blob, takeout_id integer, tmp_auth_key blob)"
        )
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            (dc, host, port, sqlite3.Binary(key), None, None),
        )
        con.commit()
    finally:
        con.close()
    return path


class TelethonSessionTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="tgup_adopt_"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.source = make_telethon_session(self.work / "py.session")
        self.dest = self.work / "tgup.session"

        saved = {
            key: os.environ.get(key)
            for key in ("TGUP_SESSION", "TGUP_TELETHON_SESSION")
        }

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ["TGUP_TELETHON_SESSION"] = str(self.source)
        os.environ["TGUP_SESSION"] = str(self.dest)

    # --- the file format -------------------------------------------------

    def test_reads_the_key_the_way_telethon_stores_it(self):
        info = telethon_session.read_auth_key(self.source)
        self.assertEqual(info["dc"], 5)
        self.assertEqual(info["addr"], "91.108.56.185:443")
        self.assertEqual(info["auth_key"], KEY)

    def test_key_id_is_the_last_eight_bytes_of_sha1(self):
        # gotd checks exactly this in restoreConnection: a mismatch is
        # "corrupted key", which looks nothing like a login problem.
        self.assertEqual(
            telethon_session.auth_key_id(KEY), hashlib.sha1(KEY).digest()[-8:]
        )

    def test_gotd_writes_the_same_key_id_rule(self):
        """Pin the format against gotd's own output, not just our belief.

        The repo's tgup.session was written by the Go binary; if this passes,
        our document is one it can read.
        """
        real = ROOT / "tgup.session"
        if not real.is_file():
            self.skipTest("no tgup.session on this machine")
        data = json.loads(real.read_text(encoding="utf-8"))
        stored = base64.b64decode(data["Data"]["AuthKey"])
        self.assertEqual(
            data["Data"]["AuthKeyID"],
            base64.b64encode(telethon_session.auth_key_id(stored)).decode(),
        )

    def test_import_writes_a_version_1_document(self):
        result = telethon_session.import_into(self.dest, self.source)
        self.assertTrue(result["imported"], result)

        data = json.loads(self.dest.read_text(encoding="utf-8"))
        self.assertEqual(data["Version"], 1)
        self.assertEqual(data["Data"]["DC"], 5)
        self.assertEqual(data["Data"]["Addr"], "91.108.56.185:443")
        self.assertEqual(
            data["Data"]["AuthKeyID"],
            base64.b64encode(telethon_session.auth_key_id(KEY)).decode(),
        )
        # Nothing of ours may invent a config: gotd fetches its own.
        self.assertEqual(data["Data"]["Config"], {})

    def test_import_keeps_the_destination_config(self):
        self.dest.write_text(json.dumps({
            "Version": 1,
            "Data": {"Config": {"ThisDC": 5, "DCOptions": [{"ID": 5}]},
                     "DC": 5, "Addr": "", "AuthKey": "", "AuthKeyID": "",
                     "Salt": 9},
        }), encoding="utf-8")

        telethon_session.import_into(self.dest, self.source)
        data = json.loads(self.dest.read_text(encoding="utf-8"))
        self.assertEqual(data["Data"]["Config"]["ThisDC"], 5)
        self.assertEqual(data["Data"]["Salt"], 0)

    def test_reimporting_the_same_key_changes_nothing(self):
        telethon_session.import_into(self.dest, self.source)
        first = self.dest.read_bytes()
        result = telethon_session.import_into(self.dest, self.source)
        self.assertFalse(result["imported"])
        self.assertEqual(self.dest.read_bytes(), first)

    def test_a_different_key_is_backed_up_and_the_old_verdict_dropped(self):
        self.dest.write_text(json.dumps({
            "Version": 1,
            "Data": {"Config": {}, "DC": 2, "Addr": "",
                     "AuthKey": base64.b64encode(bytes(256)).decode(),
                     "AuthKeyID": "stale", "Salt": 0},
        }), encoding="utf-8")
        telethon_session.verdict_path(self.dest).write_text(
            json.dumps({"checked_at": "2026-10-09T13:58:01+00:00",
                        "authorized": True, "detail": ""}),
            encoding="utf-8",
        )

        telethon_session.import_into(self.dest, self.source)

        backup = self.dest.with_name(self.dest.name + ".bak")
        self.assertTrue(backup.is_file(), "the replaced session must be kept")
        self.assertIn('"AuthKeyID": "stale"', backup.read_text(encoding="utf-8"))
        self.assertFalse(
            telethon_session.verdict_path(self.dest).exists(),
            "a verdict about the old key must not vouch for the new one",
        )

    def test_a_session_without_a_key_is_refused(self):
        empty = sqlite3.connect(self.work / "empty.session")
        empty.execute(
            "CREATE TABLE sessions (dc_id integer primary key, "
            "server_address text, port integer, auth_key blob)"
        )
        empty.commit()
        empty.close()
        with self.assertRaises(telethon_session.TelethonSessionError):
            telethon_session.read_auth_key(self.work / "empty.session")

    # --- the bridge hook -------------------------------------------------

    def test_a_foreign_session_path_is_left_alone(self):
        """$TGUP_SESSION names someone else's session: do not rewrite it."""
        os.environ.pop("TGUP_TELETHON_SESSION", None)
        self.assertEqual(tgup_bridge.ensure_session(), "")
        self.assertFalse(self.dest.exists())

    def test_the_bridge_adopts_the_python_login_once(self):
        note = tgup_bridge.ensure_session()
        self.assertIn("adopted the Python login", note)
        self.assertTrue(self.dest.is_file())
        # Second call: same key already there, nothing more to say.
        self.assertEqual(tgup_bridge.ensure_session(), "")

    def test_disabling_the_carry_over_is_honoured(self):
        os.environ["TGUP_TELETHON_SESSION"] = "off"
        self.assertEqual(tgup_bridge.ensure_session(), "")
        self.assertFalse(self.dest.exists())

    def test_no_python_login_means_the_refusal_still_stands(self):
        os.environ["TGUP_TELETHON_SESSION"] = str(self.work / "nope.session")
        self.assertEqual(tgup_bridge.ensure_session(), "")
        self.assertFalse(self.dest.exists())
        self.assertTrue(
            tgup_bridge.session_refusal("upload"),
            "with nothing to adopt, a missing session must still refuse",
        )

    def test_adopted_session_lifts_the_refusal(self):
        tgup_bridge.ensure_session()
        self.assertEqual(tgup_bridge.session_refusal("upload"), "")
        self.assertFalse(tgup_bridge.needs_login())


if __name__ == "__main__":
    unittest.main(verbosity=2)
