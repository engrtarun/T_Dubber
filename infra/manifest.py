"""The manifest, and what it says about the three Telegram hops.

The problem this exists to solve
-------------------------------
T_Dubber touches Telegram from three places, and until now each one learned
where its session file lived by a different mechanism:

1. the Python/Telethon client -- ``telegram_uploader_session.session``, beside
   the repo, overridable with ``$TGUP_TELETHON_SESSION``
2. the local Go ``tgup`` -- ``tgup.session``, overridable with ``$TGUP_SESSION``
3. the Kaggle worker -- ``/kaggle/working/tgup.session``, written as an ENV
   line in the Dockerfile and nowhere else

Hop 2's carry-over was solved once and for all by ``telethon_session.py``,
which copies the 256-byte auth key instead of asking Telegram for a new one.
Hop 3 was never given that treatment: nothing puts the key on the kernel, so
the Go binary finds no session, decides it must log in, dispatches a login code
the moment the flow starts, and a machine with no terminal can only end in EOF.
The Dockerfile says so in as many words, eleven lines above the ENV that causes
it.

Nothing here can fix that by itself -- a session file still has to reach the
kernel. What this module does is make the wiring *declared once* and
*checkable*, so that the failure is a named, actionable line before the kernel
starts rather than an EOF twenty minutes into a run.

The check that matters most is :func:`check`'s divergence finding. Every hop's
path has both a declared value here and a live environment variable, and the
environment wins at run time. When the two disagree -- which is exactly what
happened when hop 3 got its path from a Dockerfile ENV and nothing else -- the
manifest is the thing that has stopped being true, and this reports it.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from telethon_session import TelethonSessionError, auth_key_id, read_auth_key

from infra.hcl import HclDocument, HclError, load_file

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "OK",
    "Finding",
    "Hop",
    "Manifest",
    "check",
    "default_manifest_path",
    "key_id_of",
    "load_manifest",
    "read_key_id",
    "render",
    "resolve_hop_path",
    "telethon_key_id",
]

OK = "ok"

_SEVERITY_ORDER = ("error", "warn", "info")

# Values of an override that mean "no override", matching telethon_session's
# own list so that $TGUP_TELETHON_SESSION=off behaves the same everywhere.
_DISABLED = frozenset({"0", "off", "none", "false", "no", "-"})


@dataclass(frozen=True)
class Hop:
    """One place that needs a Telegram session file."""

    name: str
    env_var: str
    declared_path: str
    carrier: str
    """How the file is expected to get there: ``local`` or ``kaggle``."""
    session_format: str
    """``telethon`` (a SQLite file) or ``gotd`` (a JSON document).

    These are genuinely different on disk -- Telethon keeps the key in a SQLite
    table, gotd keeps it base64-encoded in a small JSON -- so one reader cannot
    serve both. Hop 1 is ``telethon``; hops 2 and 3 are ``gotd``, because
    ``telethon_session.py`` has already converted them by the time they matter.
    """


@dataclass(frozen=True)
class Finding:
    """One thing the manifest and the machine disagree about."""

    hop: str
    status: str
    detail: str
    fix: str

    @property
    def severity(self) -> str:
        """``error``, ``warn`` or ``info``. Never anything else."""
        if self.status in ("missing", "unreadable", "corrupt", "diverges"):
            return "error"
        if self.status == "key_mismatch":
            return "warn"
        return "info"


@dataclass(frozen=True)
class Manifest:
    """``manifest.auto.tfvars``, parsed and given meaning."""

    origin: str
    channel: str
    hops: tuple[Hop, ...]
    kaggle_dataset: str
    kaggle_private: bool
    edge_url: str

    def hop(self, name: str) -> Hop:
        """The hop called ``name``.

        Raises ``KeyError`` rather than returning a default: a typo in a hop
        name means the manifest and this code have drifted, which is exactly the
        class of bug this module exists to surface loudly.
        """
        for candidate in self.hops:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.origin} declares no hop named {name!r}")


def default_manifest_path(root: Path | None = None) -> Path:
    """Where the manifest is expected to live."""
    base = root if root is not None else Path(__file__).resolve().parent.parent
    return base / "infra" / "manifest.auto.tfvars"


def _mapping(value: object, key: str, origin: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HclError(f"{key!r} must be an object", origin, 0)
    out: dict[str, object] = {}
    for name, item in value.items():
        if isinstance(name, str):
            out[name] = item
    return out


def _text(container: Mapping[str, object], key: str, origin: str) -> str:
    value = container.get(key, "")
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def load_manifest(path: Path | None = None) -> Manifest:
    """Parse the manifest. Missing file is an error, not an empty manifest.

    A silently empty manifest would make :func:`check` report a clean machine
    and prove nothing, which is the one outcome worse than a red check.
    """
    target = path if path is not None else default_manifest_path()
    if not target.is_file():
        raise HclError(
            f"manifest not found at {target}; it is the declared source of truth",
            str(target),
            0,
        )
    document: HclDocument = load_file(target)
    attrs = document.body.attributes
    origin = str(target)

    telegram = _mapping(attrs.get("telegram"), "telegram", origin)
    kaggle = _mapping(attrs.get("kaggle"), "kaggle", origin)
    edge = _mapping(attrs.get("edge"), "edge", origin)

    hops = (
        Hop(
            name="telethon",
            env_var="TGUP_TELETHON_SESSION",
            declared_path=_text(telegram, "telethon_session", origin),
            carrier="local",
            session_format="telethon",
        ),
        Hop(
            name="local_gotd",
            env_var="TGUP_SESSION",
            declared_path=_text(telegram, "local_session", origin),
            carrier="local",
            session_format="gotd",
        ),
        Hop(
            name="kaggle",
            env_var="TGUP_SESSION",
            declared_path=_text(kaggle, "session_path", origin),
            carrier="kaggle",
            session_format="gotd",
        ),
    )
    empty = tuple(hop for hop in hops if not hop.declared_path)
    if empty:
        names = ", ".join(hop.name for hop in empty)
        raise HclError(f"these hops have no declared path: {names}", origin, 0)

    return Manifest(
        origin=origin,
        channel=_text(telegram, "channel", origin),
        hops=hops,
        kaggle_dataset=_text(kaggle, "dataset_slug", origin),
        kaggle_private=bool(kaggle.get("private", True)),
        edge_url=_text(edge, "url", origin),
    )


def _override(environ: Mapping[str, str], hop: Hop) -> str:
    """The live environment override for ``hop``, or '' when there is none."""
    raw = environ.get(hop.env_var, "").strip()
    if raw and raw.lower() not in _DISABLED:
        return raw
    return ""


def resolve_hop_path(
    manifest: Manifest,
    hop: Hop,
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Where ``hop``'s session actually is, environment taking precedence.

    The precedence is not an opinion -- it is the behaviour ``tgup_bridge.py``
    and ``telethon_session.py`` already have, because the Dockerfile sets
    ``TGUP_SESSION`` and an env var that is ignored would be a manifest that
    lies with a straight face.
    """
    env: Mapping[str, str] = environ if environ is not None else os.environ
    override = _override(env, hop)
    chosen = override or hop.declared_path
    candidate = Path(chosen)
    return candidate if candidate.is_absolute() else root / candidate


def read_key_id(path: Path) -> str | None:
    """The ``AuthKeyID`` already stored in a gotd session document.

    Reads the value the Go binary will check, rather than re-deriving it from
    the auth key, so this compares like with like: a mismatch means the two
    files really do hold different keys, not that two different pieces of code
    disagree about what a key id is.

    Total by contract -- it returns None rather than raising for anything it
    cannot read. That is not defensiveness for its own sake: this function is
    pointed at every hop, including a Telethon SQLite file, and a decode error
    escaping from a pre-flight check is a traceback where a diagnosis belongs.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    data = document.get("Data")
    if not isinstance(data, dict):
        return None
    key_id = data.get("AuthKeyID")
    return key_id if isinstance(key_id, str) and key_id else None


def telethon_key_id(path: Path) -> str | None:
    """The auth key id of a Telethon SQLite session, or None.

    Delegates to ``telethon_session.read_auth_key`` rather than opening the
    SQLite file here. That module already encodes the one rule that matters --
    which 256 bytes are the key, and that ``dc_id`` must be sane -- and a second
    implementation of it would be free to drift, which is the entire failure
    mode this directory exists to remove.
    """
    try:
        info = read_auth_key(path)
    except TelethonSessionError:
        return None
    return base64.b64encode(auth_key_id(info["auth_key"])).decode("ascii")


def key_id_of(hop: Hop, path: Path) -> str | None:
    """This hop's auth key id, however that hop stores its session.

    The two formats deliberately produce the same base64 string: gotd's
    ``AuthKeyID`` is ``b64(sha1(auth_key)[-8:])``, and so is
    :func:`telethon_key_id`. That is what makes a cross-format comparison
    meaningful -- hop 1 can be checked against hop 2 even though one is SQLite
    and the other is JSON, because the value they both yield describes the same
    256 bytes.

    Printing this value is safe, and it is worth saying why rather than
    truncating it and implying otherwise. The auth key is 256 random bytes and
    an id is eight bytes of its SHA-1 digest; recovering one from the other is
    not something anyone can do. The id is an *identifier*, and it is
    genuinely useful to show in full because "same id" is exactly the claim the
    report is making.
    """
    if hop.session_format == "telethon":
        return telethon_key_id(path)
    return read_key_id(path)


def check(
    manifest: Manifest,
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[Finding, ...]:
    """Compare the declared wiring with the machine. Never raises, never logs.

    Returns findings rather than printing them so the same function serves the
    CLI, a test, and a pre-flight hook. The rules are deliberately conservative:
    every ``error`` here has a concrete fix attached, because a pre-flight that
    says "broken" without saying what to type has moved the problem, not solved
    it.
    """
    env: Mapping[str, str] = environ if environ is not None else os.environ
    findings: list[Finding] = []
    reference_id: str | None = None

    # The local gotd session is the reference: it is the one hop that is
    # guaranteed to exist on a working machine and to have been produced by
    # telethon_session.py from the Python login.
    reference = manifest.hop("local_gotd")
    reference_path = resolve_hop_path(manifest, reference, root, env)
    if reference_path.is_file():
        reference_id = key_id_of(reference, reference_path)

    for hop in manifest.hops:
        override = _override(env, hop)
        declared = Path(hop.declared_path)
        resolved = resolve_hop_path(manifest, hop, root, env)

        if override and not declared.is_absolute():
            findings.append(
                Finding(
                    hop=hop.name,
                    status="diverges",
                    detail=(
                        f"${hop.env_var}={override!r} overrides the declared "
                        f"relative path {hop.declared_path!r}"
                    ),
                    fix=(
                        f"set ${hop.env_var} to the absolute path, or drop it "
                        f"and let the manifest decide"
                    ),
                )
            )
        elif override and Path(override) != declared:
            findings.append(
                Finding(
                    hop=hop.name,
                    status="diverges",
                    detail=(
                        f"${hop.env_var}={override!r} but the manifest "
                        f"declares {hop.declared_path!r}"
                    ),
                    fix=(
                        f"make them agree: edit infra/manifest.auto.tfvars or unset ${hop.env_var}"
                    ),
                )
            )

        if not resolved.is_file():
            findings.append(
                Finding(
                    hop=hop.name,
                    status="missing",
                    detail=f"no session file at {resolved}",
                    fix=_missing_fix(hop),
                )
            )
            continue

        key_id = key_id_of(hop, resolved)
        if key_id is None:
            findings.append(
                Finding(
                    hop=hop.name,
                    status="corrupt",
                    detail=f"{resolved} is not a readable gotd session document",
                    fix=("delete it and re-run: python telethon_session.py --dest <path>"),
                )
            )
            continue

        if reference_id and key_id != reference_id:
            findings.append(
                Finding(
                    hop=hop.name,
                    status="key_mismatch",
                    detail=(
                        f"carries auth key id {key_id}, the local session carries {reference_id}"
                    ),
                    fix=(
                        "re-run: python telethon_session.py --dest "
                        f"{resolved} so this hop carries the same key"
                    ),
                )
            )
            continue

        findings.append(
            Finding(
                hop=hop.name,
                status=OK,
                detail=f"{resolved} carries auth key id {key_id}",
                fix="",
            )
        )

    if manifest.kaggle_dataset and not manifest.kaggle_private:
        findings.append(
            Finding(
                hop="kaggle",
                status="diverges",
                detail="kaggle.private is false, so the dataset is world-readable",
                fix=("an MTProto auth key is the account: set private = true (see SECURITY.md)"),
            )
        )
    return tuple(findings)


def _missing_fix(hop: Hop) -> str:
    """The one command that creates this hop's session file."""
    if hop.carrier == "kaggle":
        return "run: python -m infra push  (publishes this key as a private Kaggle dataset)"
    if hop.name == "telethon":
        return "run: python login_once.py  (one interactive OTP, for this account only)"
    return (
        f"run: python telethon_session.py --dest {hop.declared_path} "
        "(copies the Python login across; no OTP)"
    )


def render(manifest: Manifest, findings: Sequence[Finding]) -> str:
    """Format findings as the text report the CLI prints.

    Kept separate from :func:`check` so that the ordering logic -- errors first,
    so a human reading the top line reads the thing that is broken -- is
    testable without capturing stdout.
    """
    lines: list[str] = []
    lines.append(f"manifest : {manifest.origin}")
    lines.append(f"channel  : {manifest.channel or '<not set>'}")
    lines.append(
        f"kaggle   : {manifest.kaggle_dataset or '<not set>'} "
        f"(private={str(manifest.kaggle_private).lower()})"
    )
    lines.append(f"edge     : {manifest.edge_url or '<not set>'}")
    lines.append("")

    ranked = sorted(
        findings,
        key=lambda item: (
            _SEVERITY_ORDER.index(item.severity),
            [hop.name for hop in manifest.hops].index(item.hop)
            if item.hop in [hop.name for hop in manifest.hops]
            else len(manifest.hops),
        ),
    )
    for finding in ranked:
        lines.append(f"[{finding.severity:5}] {finding.hop}: {finding.status}")
        lines.append(f"          {finding.detail}")
        if finding.fix:
            lines.append(f"          fix: {finding.fix}")
    return "\n".join(lines)
