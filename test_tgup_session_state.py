"""Session-state tri-state for the Go path (NIMBU's `check` question).

$TGUP_SESSION is pointed at a scratch dir, so the real tgup.session
-- and its verdict -- is never touched.

Run with:  python -m pytest test_tgup_session_state.py -q
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import tgup_bridge


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="tgup_state_"))
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.saved = os.environ.get("TGUP_SESSION")
        os.environ["TGUP_SESSION"] = str(self.scratch / "tgup.session")
        self.addCleanup(self._unset_session)

    def _unset_session(self):
        if self.saved is None:
            os.environ.pop("TGUP_SESSION", None)
        else:
            os.environ["TGUP_SESSION"] = self.saved

    def test_no_session_file_reports_no_file(self):
        self.assertEqual(tgup_bridge.session_state()["state"], "no_file")

    def test_present_file_without_a_verdict_is_unverified(self):
        tgup_bridge.session_path().write_bytes(b"")
        self.assertEqual(
            tgup_bridge.session_state()["state"], "present_unverified")

    def test_a_successful_round_trip_is_cached(self):
        tgup_bridge.session_path().write_bytes(b"")
        tgup_bridge.record_session_state(True)
        state = tgup_bridge.session_state()
        self.assertEqual(state["state"], "verified_ok")
        self.assertTrue(state["checked_at"])

    def test_a_rejected_session_is_kept_not_silently_dropped(self):
        tgup_bridge.session_path().write_bytes(b"")
        tgup_bridge.record_session_state(False, "not authorized yet")
        state = tgup_bridge.session_state()
        self.assertEqual(state["state"], "verified_rejected")
        self.assertEqual(state["detail"], "not authorized yet")

    def test_a_stale_verdict_expires(self):
        tgup_bridge.session_path().write_bytes(b"")
        stale = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(
                seconds=tgup_bridge.SESSION_STATE_TTL_SECONDS + 60)
        )
        tgup_bridge.session_state_path().write_text(
            json.dumps({"checked_at": stale.isoformat(),
                        "authorized": True, "detail": ""}),
            encoding="utf-8",
        )
        self.assertEqual(
            tgup_bridge.session_state()["state"], "present_unverified")

    def test_a_garbage_cache_is_unverified_not_a_crash(self):
        tgup_bridge.session_path().write_bytes(b"")
        tgup_bridge.session_state_path().write_text("{not json",
                                                   encoding="utf-8")
        self.assertEqual(
            tgup_bridge.session_state()["state"], "present_unverified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
