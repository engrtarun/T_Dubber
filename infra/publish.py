"""Publishing the auth key to the Kaggle worker. Hop 3, the missing one.

What this is for
----------------
Hop 1 (Python) and hop 2 (local Go ``tgup``) were solved once by
``telethon_session.py``: it copies the 256-byte MTProto auth key into gotd's
own format so the Go binary never has to log in and never spends an OTP.
Hop 3 -- the Go binary on the Kaggle worker -- was never given that treatment.
Nothing put the key on the kernel, so the binary found no session, decided it
must authenticate, dispatched a login code the moment the auth flow started,
and a machine with no terminal to type into could only end in EOF. The
Dockerfile documents this failure eleven lines above the ENV line that causes
it.

This module is the missing step: stage the *already authorised* local session
into a Kaggle dataset, as a private one.

What this is not
----------------
It is not a login. It never prompts, never reads an OTP, and has no code path
that can. The key it ships was created once, interactively, by ``login_once.py``.
If the local session is missing or unauthorised this refuses -- it does not
fall back to trying to log in, because that fallback is precisely the bug.

The gate
--------
``publish`` refuses to stage the key for any dataset that is not private. An
MTProto auth key is the account: ``SECURITY.md`` records that anyone holding
the file can connect as you with no OTP and no password. So ``private = true``
is a precondition checked here rather than a comment in a file nobody re-reads.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from infra.hcl import HclError
from infra.manifest import (
    Manifest,
    read_key_id,
    resolve_hop_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    # A runner takes an argv list and returns (exit code, combined output).
    # It exists so tests never shell out -- and so the one subprocess call in
    # this module cannot be triggered for real by a test.
    Runner = Callable[[Sequence[str]], "tuple[int, str]"]

__all__ = [
    "DatasetPlan",
    "PublishResult",
    "build_dataset_dir",
    "publish",
]

_KAGGLE = "kaggle"


@dataclass(frozen=True)
class DatasetPlan:
    """A staged directory holding exactly one file and its metadata."""

    directory: Path
    slug: str
    is_private: bool
    session_file: Path
    key_id: str
    metadata: dict[str, object]

    def files(self) -> tuple[Path, ...]:
        """Every staged file, session first, for the report."""
        return (self.session_file, self.directory / "dataset-metadata.json")


@dataclass(frozen=True)
class PublishResult:
    """What happened, in words a caller can print without re-deriving them."""

    ok: bool
    argv: tuple[str, ...] | None
    detail: str


def build_dataset_dir(
    manifest: Manifest,
    staging: Path,
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> DatasetPlan:
    """Stage the local session into ``staging``, ready for the Kaggle CLI.

    Raises :class:`HclError` for anything that must not be published. Every
    refusal names the fix, because a publish that says "no" without saying why
    is indistinguishable from a publish that says "no" because it broke.

    Note what is *not* staged: no source, no notebook, no config.json, no
    ``.session`` from any other hop. One file, the one the worker cannot run
    without, and nothing else -- so the blast radius of this dataset leaking is
    bounded by what the worker genuinely requires.
    """
    if not manifest.kaggle_dataset:
        raise HclError(
            "kaggle.dataset_slug is empty in the manifest",
            manifest.origin,
            0,
        )
    if not manifest.kaggle_private:
        raise HclError(
            "refusing to stage an MTProto auth key for a non-private dataset; "
            "an auth key IS the account (SECURITY.md)",
            manifest.origin,
            0,
        )

    source_hop = manifest.hop("local_gotd")
    source = resolve_hop_path(manifest, source_hop, root, environ)
    if not source.is_file():
        raise HclError(
            f"no authorised local session at {source}; run "
            f"`python telethon_session.py --dest {source}` first",
            manifest.origin,
            0,
        )
    key_id = read_key_id(source)
    if key_id is None:
        raise HclError(
            f"{source} is not a readable gotd session document; delete it and "
            "re-run telethon_session.py",
            manifest.origin,
            0,
        )

    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / source.name
    shutil.copy2(source, staged)

    # Kaggle reads `isPrivate` from the metadata file, and also accepts a
    # --private flag on create. Both are set deliberately: the flag and the file
    # disagreeing is a known way to end up with a public dataset that looked
    # private in the log.
    metadata: dict[str, object] = {
        "title": "T_Dubber session",
        "id": manifest.kaggle_dataset,
        "licenses": [{"name": "other"}],
        "keywords": ["tdubber"],
        "isPrivate": True,
    }
    (staging / "dataset-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    return DatasetPlan(
        directory=staging,
        slug=manifest.kaggle_dataset,
        is_private=True,
        session_file=staged,
        key_id=key_id,
        metadata=metadata,
    )


def _default_runner(argv: Sequence[str]) -> tuple[int, str]:
    """Run the Kaggle CLI. argv list, never shell=True (SECURITY.md)."""
    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def publish(
    plan: DatasetPlan,
    runner: Runner | None = None,
    *,
    create_only: bool = False,
) -> PublishResult:
    """Push the staged dataset with the Kaggle CLI.

    ``create_only`` picks between ``datasets create`` (first time, or the slug
    does not exist yet) and ``datasets version`` (everything after that). It is
    a parameter rather than a second command because Kaggle's create is not
    idempotent -- running it twice fails -- and picking the wrong one by
    guessing would turn a retry into an error the user has to read to decode.
    """
    if not plan.is_private:
        return PublishResult(
            ok=False,
            argv=None,
            detail="refusing to publish a non-private dataset holding an auth key",
        )

    verb = "create" if create_only else "version"
    argv: tuple[str, ...] = (
        _KAGGLE,
        "datasets",
        verb,
        "-p",
        str(plan.directory),
        "--dir-mode",
        "tar",
        "--private",
    )
    if verb == "version":
        argv += ("-m", "t_dubber session carry-over")

    run: Runner = runner if runner is not None else _default_runner
    code, output = run(argv)
    if code != 0:
        return PublishResult(
            ok=False,
            argv=argv,
            detail=(
                f"`{_KAGGLE} datasets {verb}` exited {code}. If the dataset "
                "does not exist yet, run this once with --create."
            )
            + (f"\n{output.strip()}" if output.strip() else ""),
        )
    return PublishResult(
        ok=True,
        argv=argv,
        detail=(f"staged and published {plan.slug} (private, auth key id {plan.key_id})"),
    )
