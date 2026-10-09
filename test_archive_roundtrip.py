"""ISSUE 3: the archive index's writer and reader must agree.

Older tests seeded the database directly, so they only verified
their own format. These go through the real writer
(``app._archive_to_db``) and the real reader (Tier 0's
``app._find_archived_copy``) with the same digest, on a scratch
database -- the real t_dubber.db is never touched (same trick
as test_db.py and InFlightChannelTests).

Journal keys are the *uploader's* keys (``message_link``,
``message_id``): that is what ``db.upsert_archive`` reads, and
a test that writes different keys only verifies its own format.

Run with:  python -m pytest test_archive_roundtrip.py -q
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db


def _use_temp_db(path):
    """Point db at a scratch file so the real database is never touched."""
    original = db.DB_PATH
    db.DB_PATH = path
    if getattr(db._local, "conn", None) is not None:
        db._local.conn.close()
        db._local.conn = None
    db.connect()
    return original


def _restore_db(original):
    if getattr(db._local, "conn", None) is not None:
        db._local.conn.close()
        db._local.conn = None
    db.DB_PATH = original


class ArchiveRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="p0_roundtrip_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.original_db = _use_temp_db(str(self.scratch / "t_dubber.db"))
        self.addCleanup(_restore_db, self.original_db)

    def _import_app(self):
        try:
            import app
        except Exception as exc:  # noqa: BLE001 - gradio may be absent
            self.skipTest(f"app import unavailable: {exc}")
        return app

    def test_writer_then_reader_round_trips_the_link(self):
        """What _archive_to_db writes, _find_archived_copy reads back."""
        app = self._import_app()
        digest = "a" * 64
        archive_id = app._archive_to_db(
            {
                "filename": "movie.mkv",
                "channel": "@tgwebcloud1",
                "state": "complete",
                "message_link": "https://t.me/tgwebcloud1/42",
                "message_id": 42,
            },
            project_id=None,
            content_sha256=digest,
        )
        self.assertIsNotNone(archive_id)
        self.assertEqual(
            app._find_archived_copy(digest, "@tgwebcloud1"),
            "https://t.me/tgwebcloud1/42",
        )

    def test_inflight_upload_is_not_a_copy(self):
        """An in-flight snapshot must never satisfy a Tier 0 lookup."""
        app = self._import_app()
        digest = "b" * 64
        app._archive_to_db(
            {
                "filename": "movie.mkv",
                "channel": "@tgwebcloud1",
                "state": "uploading",
            },
            content_sha256=digest,
        )
        self.assertIsNone(app._find_archived_copy(digest, "@tgwebcloud1"))

    def test_other_channel_does_not_hit(self):
        """The cache is per-channel: another channel's copy is not ours."""
        app = self._import_app()
        digest = "c" * 64
        app._archive_to_db(
            {
                "filename": "movie.mkv",
                "channel": "@tgwebcloud1",
                "state": "complete",
                "message_link": "https://t.me/tgwebcloud1/7",
            },
            content_sha256=digest,
        )
        self.assertIsNone(app._find_archived_copy(digest, "@otherchannel"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
