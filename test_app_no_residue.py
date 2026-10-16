"""Zero-residue guarantees in app.py: the scratch-inbox wrappers + startup sweep.

Two failure modes used to leak ``projects/_inbox/<stamp>`` forever, and neither
shows up as a failing test unless it is pinned down here:

1. **GeneratorExit.** The Gradio event handlers are generators. When the user
   closes the tab (or hits stop) mid-download, Python throws ``GeneratorExit``
   into the ``yield from`` -- a plain ``try/except`` around the body never runs.
   Only ``finally`` sees it, which is why the cleanup lives in the wrapper's
   ``finally`` (``start_dubbing`` / ``do_telegram_upload_from_link``) and not in
   the impl.
2. **Hard kills.** A Task-Manager kill never runs any of that -- the startup
   sweep (``_sweep_stale_inbox``) is the only reaper, and its age gate is the
   only thing standing between "cleanup" and "deleting the download that is
   running right now".

Every function here is annotated: the repo's mypy override for ``test_*``
modules is currently an invalid pattern (mypy rejects ``test_*`` and drops the
section), so root test files are checked under full strict -- see the note in
AGENTS_MAP.md.

Run with:  python test_app_no_residue.py
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from testkit import ScratchWorkspace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Redirect the database BEFORE app is imported: importing app builds the whole
# Gradio page, and its module-level ``load_project_history()`` seeds the
# database when it finds no projects. Same reasoning as test_flow_order.py;
# conftest.py repeats the redirect per test so a restore elsewhere in the
# suite cannot point this module back at production mid-run.
import test_isolation

test_isolation.use_scratch_db(prefix="tdub_app_residue_")

import app

# A fake _start_dubbing_impl must have the same shape the wrapper calls with:
# six positional arguments plus the scratch handoff dict.
DubbingStub = Callable[..., Generator[tuple[str, str], None, None]]
DriveStub = Callable[..., Generator[str, None, None]]


def _make_inbox(projects_dir: str, name: str, age_hours: float) -> str:
    """Create ``projects/_inbox/<name>`` whose newest content is age_hours old.

    Both the file and the directory get the old timestamp: the sweep takes the
    MAX of the two, so touching only one would make the test pass for the wrong
    reason.
    """
    inbox_root = os.path.join(projects_dir, "_inbox")
    os.makedirs(inbox_root, exist_ok=True)
    inbox = os.path.join(inbox_root, name)
    os.makedirs(inbox)
    payload = os.path.join(inbox, "clip.part")
    with open(payload, "wb") as handle:
        handle.write(b"partial-bytes")
    stamp = time.time() - age_hours * 3600
    os.utime(payload, (stamp, stamp))
    os.utime(inbox, (stamp, stamp))
    return inbox


def _dubbing_stub(projects_dir: str, mode: str) -> tuple[DubbingStub, str]:
    """Fake ``_start_dubbing_impl`` that registers an inbox like the real one.

    ``mode`` selects the exit path being tested: ``"hang"`` yields once and
    waits (the caller closes it), ``"raise"`` blows up after registering, and
    ``"return"`` takes the early-error-return path.
    """
    inbox = os.path.join(projects_dir, "_inbox", "stamp_dub")

    def _impl(
        video_file: object,
        target_lang: object,
        speaker_detection: object,
        source_url: str = "",
        backup_to_telegram: bool = True,
        archive_only: bool = False,
        scratch: dict[str, str] | None = None,
    ) -> Generator[tuple[str, str], None, None]:
        if scratch is None:
            raise AssertionError("the wrapper must pass its scratch dict")
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "link.part"), "wb") as handle:
            handle.write(b"partial-download")
        scratch["inbox"] = inbox
        if mode == "raise":
            raise RuntimeError("stage blew up")
        if mode == "return":
            return
        yield ("STAGE:1", "downloading")

    return _impl, inbox


def _drive_stub(projects_dir: str, mode: str) -> tuple[DriveStub, str]:
    """Same as :func:`_dubbing_stub` for ``do_telegram_upload_from_link``.

    The Drive tab's early error returns were the leak this suite exists for:
    they used to ``return`` straight out and leave the inbox on disk.
    """
    inbox = os.path.join(projects_dir, "_inbox", "stamp_drive")

    def _impl(
        source_url: str,
        scratch: dict[str, str] | None = None,
    ) -> Generator[str, None, None]:
        if scratch is None:
            raise AssertionError("the wrapper must pass its scratch dict")
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "clip.part"), "wb") as handle:
            handle.write(b"partial-download")
        scratch["inbox"] = inbox
        if mode == "raise":
            raise RuntimeError("drive stage blew up")
        if mode == "return":
            return
        yield "<div>archived</div>"

    return _impl, inbox


# ---------------------------------------------------------------------------
# Startup sweep -- the only reaper for hard-killed runs
# ---------------------------------------------------------------------------
def test_startup_sweep_reaps_only_stale_inbox_folders(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = str(scratch_workspace.path)
    monkeypatch.setattr(app, "PROJECTS_DIR", projects_dir)
    stale = _make_inbox(projects_dir, "job_killed", age_hours=48)
    fresh = _make_inbox(projects_dir, "job_running", age_hours=1)
    # A loose file is not a folder the sweep owns -- even an old one stays.
    loose = os.path.join(projects_dir, "_inbox", "not_a_dir.bin")
    with open(loose, "wb") as handle:
        handle.write(b"leftover")
    old_stamp = time.time() - 48 * 3600
    os.utime(loose, (old_stamp, old_stamp))

    removed = app._sweep_stale_inbox(max_age_hours=24)

    assert removed == 1, "exactly the one stale folder should be reaped"
    assert not os.path.exists(stale), "a 48h-old killed run must be reclaimed"
    assert os.path.isdir(fresh), "a job touched 1h ago may still be downloading"
    assert os.path.isfile(loose), "loose files are not inbox folders"
    # Second pass reaps nothing: the sweep is idempotent at startup.
    assert app._sweep_stale_inbox(max_age_hours=24) == 0


def test_startup_sweep_never_blocks_launch_when_inbox_is_absent(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh machine has no projects/_inbox at all -- that must not raise."""
    monkeypatch.setattr(app, "PROJECTS_DIR", str(scratch_workspace.path))
    assert app._sweep_stale_inbox() == 0


# ---------------------------------------------------------------------------
# start_dubbing wrapper -- inbox dies on every exit path
# ---------------------------------------------------------------------------
def test_dubbing_inbox_dies_when_the_client_walks_away(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GeneratorExit (tab closed mid-run) must still reclaim the scratch."""
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _dubbing_stub(projects_dir, "hang")
    monkeypatch.setattr(app, "_start_dubbing_impl", stub)

    gen = app.start_dubbing(None, "Hindi", False)
    assert next(gen) == ("STAGE:1", "downloading")
    assert os.path.isdir(inbox), "the stub registered its inbox as the real code does"
    gen.close()  # what Gradio does when the client walks away

    assert not os.path.exists(inbox), "GeneratorExit path must run the finally"


def test_dubbing_inbox_dies_when_a_stage_raises(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _dubbing_stub(projects_dir, "raise")
    monkeypatch.setattr(app, "_start_dubbing_impl", stub)

    gen = app.start_dubbing(None, "Hindi", False)
    with pytest.raises(RuntimeError, match="stage blew up"):
        next(gen)

    assert not os.path.exists(inbox), "exception path must run the finally"


def test_dubbing_inbox_dies_on_the_early_return_path(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _dubbing_stub(projects_dir, "return")
    monkeypatch.setattr(app, "_start_dubbing_impl", stub)

    assert list(app.start_dubbing(None, "Hindi", False)) == []

    assert not os.path.exists(inbox), "early-error-return path must run the finally"


# ---------------------------------------------------------------------------
# Drive tab wrapper -- the historical leaker
# ---------------------------------------------------------------------------
def test_drive_inbox_dies_on_the_early_return_path(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the leak that motivated the wrapper: return before any yield."""
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _drive_stub(projects_dir, "return")
    monkeypatch.setattr(app, "_do_telegram_upload_from_link_impl", stub)

    assert list(app.do_telegram_upload_from_link("https://example.com/v")) == []

    assert not os.path.exists(inbox), "the Drive tab's early returns used to leak this"


def test_drive_inbox_dies_when_a_stage_raises(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _drive_stub(projects_dir, "raise")
    monkeypatch.setattr(app, "_do_telegram_upload_from_link_impl", stub)

    gen = app.do_telegram_upload_from_link("https://example.com/v")
    with pytest.raises(RuntimeError, match="drive stage blew up"):
        next(gen)

    assert not os.path.exists(inbox), "exception path must run the finally"


def test_drive_inbox_dies_when_the_client_walks_away(
    scratch_workspace: ScratchWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = str(scratch_workspace.path)
    stub, inbox = _drive_stub(projects_dir, "hang")
    monkeypatch.setattr(app, "_do_telegram_upload_from_link_impl", stub)

    gen = app.do_telegram_upload_from_link("https://example.com/v")
    assert next(gen) == "<div>archived</div>"
    assert os.path.isdir(inbox)
    gen.close()

    assert not os.path.exists(inbox), "GeneratorExit path must run the finally"


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))
