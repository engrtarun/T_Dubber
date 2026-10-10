#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch / verify / locate the llama.cpp + whisper.cpp binaries.

    python cpp_accelerator/runtimes/build_runtimes.py
    python cpp_accelerator/runtimes/build_runtimes.py --offline
    python cpp_accelerator/runtimes/build_runtimes.py --url llama-server=file:///... --sha llama-server=<hex>

WHY THIS EXISTS
---------------
Kaggle run `test4_gotgVERSION` spent 1067 s of a 1258 s run inside
``pip install vllm`` and then died with
``ImportError: libcudart.so.13`` -- vLLM 0.26.0 is built against CUDA 13
while the Kaggle image ships CUDA 12. 85% of the run, zero output.

The replacement is a handful of self-contained C++ binaries. This module's
whole job is to make sure exactly one of them is on disk, is the file we
think it is, and can be found by a one-line resolution order -- so a Kaggle
cell never has to guess.

CONTRACT (what a notebook cell parses)
--------------------------------------
Exactly one line per requested binary, nothing else on stdout:

    RUNTIME_OK      <name> <path>     present (digest verified, or unpinned)
    RUNTIME_MISSING <name> -          not found and not fetchable here
    RUNTIME_ERROR   <name> <reason>   fetch/verify failed, reason on one line

Lines starting with ``#`` are human commentary and are safe to ignore.

OFFLINE TOLERANCE
-----------------
If the binary already exists and its sha256 matches ``MANIFEST.sha256``,
everything above is a no-op: no network, no download, no rewrite. That is the
warm-run path and it must stay free -- it is the whole point of dropping the
pip install.

A binary that was placed by hand (CMake build, conda, /usr/bin) is accepted
as ``RUNTIME_OK`` but reported ``RUNTIME_NOTE ... unpinned``: refusing to run
an un-pinned binary would make this tool unusable locally, and refusing to
download when it is already present would make every run pay for a warm file.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Sequence

__all__ = [
    "ALIASES",
    "BINARIES",
    "BinarySpec",
    "DEFAULT_NAMES",
    "MANIFEST",
    "RUNTIMES_DIR",
    "download",
    "ensure",
    "main",
    "resolve",
    "search_dirs",
    "sha256_file",
]

# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

RUNTIMES_DIR = Path(__file__).resolve().parent          # cpp_accelerator/runtimes
ACCEL_DIR = RUNTIMES_DIR.parent                         # cpp_accelerator
BIN_DIR = RUNTIMES_DIR / "bin"
BUILD_DIR = ACCEL_DIR / "build"
MANIFEST = BIN_DIR / "MANIFEST.sha256"

#: Environment override. Highest priority in :func:`search_dirs` so a Kaggle
#: cell can point at `/kaggle/input/td_runtimes` without copying a byte.
ENV_BIN_DIR = "TDUBBER_BIN_DIR"

_CHUNK = 1 << 20

#: Archive types we know how to open. llama.cpp ships tar.gz for Linux/macOS
#: and zip for Windows; whisper.cpp ships zip for both.
ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz")


@dataclasses.dataclass(frozen=True)
class BinarySpec:
    """One logical binary and where its prebuilt build comes from."""

    name: str
    #: Candidate GitHub repos, newest org first. ggml-org is the current home;
    #: ggerganov is kept because older tags were only ever published there and
    #: a redirect is cheaper than a failed run.
    repos: tuple[str, ...]
    #: Release-asset name patterns, tried in order. The FIRST hit wins, so the
    #: CPU build is listed before the CUDA/ROCm/Vulkan ones: on a Kaggle T4 the
    #: CPU build is already fast enough for a 2B model, while a CUDA 13 build
    #: would reproduce exactly the `libcudart.so.13` import failure this whole
    #: migration exists to avoid.
    asset_patterns: tuple[str, ...]
    #: Install subdirectory name. NOT cosmetic: llama.cpp and whisper.cpp ship
    #: colliding runtime DLL names (ggml.dll, llama.dll, ggml-cpu-*.dll,
    #: ggml-base.dll) at different ABI versions. Installing both into one
    #: directory makes whichever was extracted second win, and the first
    #: binary then dies at load time with
    #: "This version of %1 is not compatible with the version of Windows"
    #: (WinError 216) -- before main(), so with no log output at all.
    project: str = "llama"
    #: Historic names that still mean "this binary".
    aliases: tuple[str, ...] = ()
    required: bool = True


BINARIES: tuple[BinarySpec, ...] = (
    BinarySpec(
        name="llama-server",
        repos=("ggml-org/llama.cpp", "ggerganov/llama.cpp"),
        asset_patterns=("bin-win-cpu-x64.zip", "bin-ubuntu-x64.tar.gz",
                        "bin-macos-x64.tar.gz", "bin-win-cpu-x64", "ubuntu-x64"),
    ),
    BinarySpec(
        name="llama-cli",
        repos=("ggml-org/llama.cpp", "ggerganov/llama.cpp"),
        asset_patterns=("bin-win-cpu-x64.zip", "bin-ubuntu-x64.tar.gz",
                        "bin-macos-x64.tar.gz", "bin-win-cpu-x64", "ubuntu-x64"),
        required=False,
    ),
    BinarySpec(
        name="llama-quantize",
        repos=("ggml-org/llama.cpp", "ggerganov/llama.cpp"),
        asset_patterns=("bin-win-cpu-x64.zip", "bin-ubuntu-x64.tar.gz",
                        "bin-macos-x64.tar.gz", "bin-win-cpu-x64", "ubuntu-x64"),
        required=False,
    ),
    BinarySpec(
        name="whisper-cli",
        repos=("ggml-org/whisper.cpp", "ggerganov/whisper.cpp"),
        # whisper.cpp ships one zip for every platform; the Windows flavour is
        # still called whisper-cli.exe, so the same glob finds both.
        asset_patterns=("whisper-bin-x64.zip", "bin-ubuntu-x64.tar.gz",
                        "bin-linux-x64", "whisper-bin-x64"),
        project="whisper",
        # `whisper-whisper-cli` is upstream's current name; `whisper-cli` and
        # `main` are the two spellings it was renamed from. Both older ones
        # still work, but whisper.cpp ships them as deprecation shims that
        # exit 1 on a stock zip -- so the newest name is matched first.
        aliases=("whisper-whisper-cli", "whisper-cpp", "main", "whisper"),
    ),
)

#: Names we resolve when the caller does not say.
DEFAULT_NAMES: tuple[str, ...] = ("llama-server", "whisper-cli")

#: legacy name -> canonical name
ALIASES: dict[str, str] = {
    alias: spec.name for spec in BINARIES for alias in spec.aliases
}

_BY_NAME: dict[str, BinarySpec] = {spec.name: spec for spec in BINARIES}


def canonical_name(name: str) -> str:
    """Map ``whisper-cpp`` / ``main`` onto the canonical ``whisper-cli``."""
    key = name.strip().lower()
    key = key[:-4] if key.endswith(".exe") else key
    return ALIASES.get(key, key)


# --------------------------------------------------------------------------- #
# Output contract
# --------------------------------------------------------------------------- #

def emit(line: str) -> None:
    print(line, flush=True)


def report(status: str, name: str, path: str | Path = "-", detail: str = "") -> None:
    """Print one machine-readable line. Never raise, never print twice."""
    parts = [status, name, str(path)]
    emit(" ".join(parts))
    if detail:
        emit(f"RUNTIME_NOTE {name} {detail}")


# --------------------------------------------------------------------------- #
# Digests
# --------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_read(manifest: Path = MANIFEST) -> dict[str, str]:
    """Parse a ``sha256sum``-style file. Unknown lines are ignored, not fatal:
    a truncated manifest must not make every binary unfindable."""
    entries: dict[str, str] = {}
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if match:
            entries[Path(match.group(2).strip()).name] = match.group(1).lower()
    return entries


def manifest_write(entries: dict[str, str], manifest: Path = MANIFEST) -> None:
    """Write digests next to the binaries. Atomic: a crash mid-write must not
    leave a manifest that matches nothing."""
    manifest.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(entries.items())
    )
    tmp = manifest.with_suffix(manifest.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, manifest)


# --------------------------------------------------------------------------- #
# Resolution order
# --------------------------------------------------------------------------- #

def _executable_names(name: str) -> list[str]:
    """``llama-server`` -> ``['llama-server']`` on POSIX,
    ``['llama-server.exe', 'llama-server']`` on Windows.

    Both spellings are probed on Windows because a CMake build tree also emits
    the extensionless copy next to the real .exe."""
    return [name + ".exe", name] if os.name == "nt" else [name]


def search_dirs(extra: Iterable[Path] = ()) -> list[Path]:
    """Directories to probe, in priority order.

    1. ``TDUBBER_BIN_DIR``   -- operator override (mounted Kaggle dataset)
    2. ``runtimes/bin/``    -- what this module downloads into
    3. ``cpp_accelerator/build*/`` -- a local CMake build
    4. extra (test hook), then PATH
    """
    dirs: list[Path] = []
    env = os.environ.get(ENV_BIN_DIR, "").strip()
    if env:
        dirs.extend(Path(part) for part in env.split(os.pathsep) if part)
    dirs.append(BIN_DIR)
    # `build*/` rather than `build/`: this repo has build, build_mix and
    # build_scalar, and a locally compiled llama-server is the fast path on
    # Windows where there is no upstream x64 zip at all.
    dirs.extend(sorted(p for p in ACCEL_DIR.glob("build*") if p.is_dir()))
    dirs.extend(extra)
    out: list[Path] = []
    for directory in dirs:
        if not directory:
            continue
        out.append(directory)
        # Per-project subdirectories take priority over the shared one: they
        # are the layout build_runtimes itself writes, and it is the only
        # layout where llama.cpp and whisper.cpp can coexist.
        try:
            out.extend(sorted(p for p in directory.iterdir()
                              if p.is_dir() and (p / "MANIFEST.sha256").exists()))
        except OSError:
            continue
    return out


def resolve(name: str, extra: Iterable[Path] = ()) -> Path | None:
    """First executable matching *name*, or ``None``.

    Every hit is checked for the executable bit: a directory called
    ``llama-server`` or a non-executable file is a bug in someone's setup, and
    returning it would produce a confusing ``PermissionError`` much later --
    from inside the bridge, minutes into a run."""
    canon = canonical_name(name)
    spec = _BY_NAME.get(canon)
    # Probe the canonical name AND every alias: upstream renamed whisper's CLI
    # to `whisper-whisper-cli`, and a build directory may hold any of the three
    # spellings. Canonical-only probing finds none of them.
    stems = [canon] + (list(spec.aliases) if spec else [])
    dir_names: list[str] = []
    for stem in stems:
        dir_names.extend(_executable_names(stem))
    if canon not in _BY_NAME:  # unknown binary: probe the raw name too
        dir_names = _executable_names(name)
    # de-duplicate, keep order
    dir_names = list(dict.fromkeys(dir_names))

    for directory in search_dirs(extra):
        try:
            for candidate in dir_names:
                path = Path(directory) / candidate
                if path.is_file() and os.access(path, os.X_OK):
                    return path
        except OSError:
            continue

    # PATH lookup uses the CANONICAL name only. Aliases are dangerous here:
    # Windows resolves `main` to C:\Windows\System32\main.CPL (a Control Panel
    # applet) through PATHEXT, so aliasing it on PATH happily "finds" a
    # system file that is not a speech recogniser at all.
    for candidate in _executable_names(canon if canon in _BY_NAME else name):
        found = shutil.which(candidate)
        if found:
            return Path(found)
    return None


# --------------------------------------------------------------------------- #
# Download (hash-checked, always)
# --------------------------------------------------------------------------- #

def _http_get(url: str, timeout: float = 120.0) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "tdubber-build-runtimes/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def download(
    url: str,
    dest: Path,
    expected_sha256: str | None = None,
    opener: Callable[[str], bytes] | None = None,
) -> Path:
    """Fetch *url* to *dest*, refusing to keep an unverified file.

    The payload lands in ``<dest>.part`` and is only renamed into place after
    the digest matched. A truncated or tampered download therefore never
    becomes a runnable binary -- an interrupt used to leave a half file that
    the next (offline) run happily "found"."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = (opener or _http_get)(url)
    data = bytearray(payload)  # zipfile needs a seekable file object
    got = hashlib.sha256(data).hexdigest()
    if expected_sha256 and got != expected_sha256.lower():
        raise ValueError(
            f"sha256 mismatch for {url}: expected {expected_sha256}, got {got}"
        )
    part = dest.with_suffix(dest.suffix + ".part")
    with open(part, "wb") as handle:
        handle.write(data)
    os.replace(part, dest)
    return dest


# --------------------------------------------------------------------------- #
# Release lookup
# --------------------------------------------------------------------------- #

def preferred_patterns(spec: BinarySpec, platform: str = "") -> tuple[str, ...]:
    """The spec's asset patterns, host platform's first.

    Both projects publish every platform on every release, so a fixed order
    wastes API calls matching assets that will never run here -- and a
    *mis*match is worse than a waste: a Windows zip on Linux extracts to a .exe
    the resolver can find but the kernel cannot run."""
    host = (platform or sys.platform).lower()
    if host.startswith("win"):
        head = [p for p in spec.asset_patterns if "-win" in p or "win-" in p]
    elif host == "darwin":
        head = [p for p in spec.asset_patterns if "macos" in p]
    else:
        head = [p for p in spec.asset_patterns if "ubuntu" in p or "linux" in p]
    tail = [p for p in spec.asset_patterns if p not in head]
    return tuple(head + tail)


def release_assets(repo: str, pattern: str, token: str | None = None) -> str:
    """URL of the newest release asset whose name matches *pattern*.

    Walks backwards from the newest release because a project that ships a new
    asset name per build (llama.cpp does: `llama-b11539-bin-ubuntu-x64.tar.gz`)
    will not match on tag 1 if the tag it did match on is old and its binary no
    longer runs. Only archives are returned: a raw single-file asset has no
    digest published next to it and no place to put one."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tdubber-build-runtimes/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for page in (1, 2):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=20&page={page}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            releases = json.loads(response.read().decode("utf-8"))
        if not releases:
            break
        for release in releases:
            if release.get("draft"):
                continue
            for asset in release.get("assets", []):
                name = asset.get("name", "")
                if pattern in name and name.endswith(ARCHIVE_SUFFIXES):
                    return asset["browser_download_url"]
    raise LookupError(f"no release asset matching {pattern!r} in {repo}")


def _extract_member(data: bytes, spec: BinarySpec, out_dir: Path) -> Path | None:
    """Pull one binary -- and its shared libraries -- out of a release archive.

    Handles zip AND tar.gz because upstream is not consistent: llama.cpp ships
    ``.tar.gz`` for Linux/macOS and ``.zip`` for Windows, while whisper.cpp
    ships ``.zip`` for both. Assuming zip is how a Linux fetch ends in
    ``tarfile.ReadError``.

    The archive is searched by BASENAME rather than by a fixed path: upstream
    moves files between ``build/bin/`` and ``build/bin/Release/`` between
    releases, and a hard-coded path is a guaranteed break on the next tag.

    Sibling DLLs come along on purpose. The Windows build is a DLL farm --
    ``llama-server.exe`` alone dies with ``STATUS_DLL_NOT_FOUND`` (exit code
    3221225781, and *no log output at all*, because it dies before main() --
    unless ``ggml.dll``, ``libomp.dll`` and the ``ggml-cpu-*.dll`` variants
    sit next to it. Extracting only the named binary produces a "resolved,
    verified, still cannot run" install, which is worse than not finding it.

    Nothing is written to its original archive path, so a malicious archive
    cannot escape out_dir."""
    buffer = io.BytesIO(bytes(data))
    if data[:2] == b"PK":
        with zipfile.ZipFile(buffer) as archive:
            members = [(info.filename, archive.open(info))
                       for info in archive.infolist() if not info.is_dir()]
            return _write_members(members, spec, out_dir)
    if data[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=buffer, mode="r:gz") as archive:
            members = []
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                handle = archive.extractfile(info)
                if handle is not None:
                    members.append((info.name, handle))
            return _write_members(members, spec, out_dir)
    raise ValueError("downloaded asset is neither a zip nor a gzip tar")


def _write_members(members, spec: BinarySpec, out_dir: Path) -> Path | None:
    """Write the target binary plus its DLLs, in two passes.

    Selection is TWO-PASS: the canonical name is looked for first and aliases
    only fill in if it is absent. A single pass over the archive picks whichever
    member happens to come first, and whisper.cpp ships ``main.exe`` (a
    deprecation shim that exits 1) alongside ``whisper-cli.exe`` with main
    sorting earlier -- so a first-match-wins extractor installs the broken one
    under the right filename and the failure looks like a code bug.

    The binary is written LAST, after its DLLs, so a concurrent resolve() can
    never see an executable that cannot load."""
    out_dir.mkdir(parents=True, exist_ok=True)

    def pick(stems: Sequence[str]) -> str | None:
        wanted_set = {n for stem in stems for n in _executable_names(stem)}
        for name, _handle in members:
            if Path(name).name in wanted_set:
                return name
        return None

    match = pick([spec.name]) or pick(spec.aliases)
    if match is None:
        return None
    base = Path(match).name
    chosen = out_dir / (spec.name + (".exe" if base.endswith(".exe") else ""))

    for name, handle in members:
        other = Path(name).name
        if other == base or not other.lower().endswith((".dll", ".so", ".so.1", ".dylib")):
            continue
        with handle, open(out_dir / other, "wb") as dst:
            shutil.copyfileobj(handle, dst)
        _make_runnable(out_dir / other)

    for name, handle in members:
        if Path(name).name == base:
            with handle, open(chosen, "wb") as dst:
                shutil.copyfileobj(handle, dst)
            break
    _make_runnable(chosen)
    return chosen


def _make_runnable(path: Path) -> None:
    """Archives do not reliably carry the x bit, and resolve() rejects a
    non-executable file -- so without this the file we just wrote is reported
    MISSING by the very next run."""
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# ensure(): the one function everything else calls
# --------------------------------------------------------------------------- #

def ensure(
    name: str,
    *,
    offline: bool = True,
    urls: dict[str, str] | None = None,
    digests: dict[str, str] | None = None,
    out_dir: Path | None = None,
    opener: Callable[[str], bytes] | None = None,
    quiet: bool = False,
) -> Path | None:
    """Return a usable path for *name*, fetching it only if that is really needed.

    Default is ``offline=True``: a Kaggle run must never silently spend minutes
    on a download it did not ask for. Pass ``offline=False`` to opt in."""
    canon = canonical_name(name)
    spec = _BY_NAME.get(canon)
    # Default install location is a per-project subdirectory, never the shared
    # bin/ root: the two projects ship colliding DLL names at different ABI
    # versions, so a shared directory yields one of them failing to load.
    target_dir = Path(out_dir) if out_dir else BIN_DIR / (spec.project if spec else canon)
    manifest = target_dir / "MANIFEST.sha256"
    pinned = manifest_read(manifest)

    path = resolve(canon, extra=[target_dir] if out_dir else ())

    if path is not None:
        expected = pinned.get(path.name)
        if expected is None:
            report("RUNTIME_OK", canon, path, "unpinned (no manifest entry)")
            return path
        actual = sha256_file(path)
        if actual == expected:
            report("RUNTIME_OK", canon, path, "sha256 ok")
            return path
        # Mismatch: a truncated download or a half-copied mounted dataset.
        if offline:
            report("RUNTIME_ERROR", canon, path, f"sha256 mismatch (got {actual[:16]})")
            return None
        # No RUNTIME_OK here: the contract is exactly one status line per
        # binary, and this one is about to be replaced. Reporting it now would
        # print RUNTIME_OK followed by RUNTIME_ERROR if the refetch failed,
        # and a caller that stops at the first match would run the bad file.
        emit(f"RUNTIME_NOTE {canon} sha256 mismatch, refetching")

    if offline:
        report("RUNTIME_MISSING", canon)
        return None

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        url = (urls or {}).get(canon)
        digest = (digests or {}).get(canon)
        if url is None:
            if spec is None:
                raise LookupError(f"unknown runtime {canon!r}")
            last: Exception | None = None
            for repo in spec.repos:
                for pattern in preferred_patterns(spec):
                    try:
                        url = release_assets(repo, pattern, token)
                        break
                    except (LookupError, urllib.error.URLError, OSError) as exc:
                        last = exc
                if url:
                    break
            if url is None:
                raise last or LookupError(f"no asset found for {canon}")
        data = (opener or _http_get)(url)
        # The pinned digest describes the downloaded artefact (the zip), not the
        # binary inside it, so it is checked BEFORE the archive is opened --
        # verifying only the extracted member would let a tampered zip through.
        actual_digest = hashlib.sha256(data).hexdigest()
        if digest and actual_digest != digest.lower():
            raise ValueError(
                f"sha256 mismatch for {url}: expected {digest}, got {actual_digest}"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        if url.endswith(ARCHIVE_SUFFIXES) or data[:2] in (b"PK", b"\x1f\x8b"):
            got = _extract_member(data, spec, target_dir)
            if got is None:
                raise LookupError(f"{url} does not contain {canon}")
        else:
            # Name it after the resolved hit when replacing one, else after the
            # canonical name -- never after the URL. A release asset is called
            # e.g. `llama-b4517-bin-ubuntu-x64.zip` or `whisper-cli-linux-x64`,
            # neither of which any resolver looks for, so naming the file after
            # it would produce a file the next run reports as MISSING. Replacing
            # on the found path also matters on Windows: the resolver probes
            # `llama-server.exe` first, so refetching into an extensionless
            # sibling would leave the bad file winning every later lookup.
            dest = path or target_dir / _executable_names(canon)[0]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            # A raw download is not executable yet, and resolve() refuses a
            # non-executable file -- so without this the very next run would
            # report the binary we just fetched as MISSING.
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            got = dest
        actual = sha256_file(got)
        pinned[got.name] = actual
        manifest_write(pinned, manifest)
        report("RUNTIME_OK", canon, got, f"downloaded sha256={actual[:16]}")
        return got
    except Exception as exc:  # noqa: BLE001 -- one line, never a traceback
        report("RUNTIME_ERROR", canon, detail=f"{type(exc).__name__}: {exc}")
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", default=None,
                        help="binaries to ensure (default: llama-server whisper-cli)")
    parser.add_argument("--offline", action="store_true", default=False,
                        help="never download; fail if a binary is missing")
    parser.add_argument("--url", action="append", default=[], metavar="NAME=URL",
                        help="pin a direct download URL (also accepts file:// URLs)")
    parser.add_argument("--sha", action="append", default=[], metavar="NAME=HEX",
                        help="expected sha256 for --url")
    args = parser.parse_args(argv)

    urls = dict(item.split("=", 1) for item in args.url if "=" in item)
    digests = dict(item.split("=", 1) for item in args.sha if "=" in item)
    names = args.names or list(DEFAULT_NAMES)

    emit(f"# platform={platform.system()} {platform.machine()} "
         f"python={sys.version.split()[0]} offline={args.offline}")
    missing = []
    for name in names:
        if ensure(name, offline=args.offline, urls=urls, digests=digests) is None:
            missing.append(name)
    if missing:
        emit(f"# missing: {', '.join(missing)} -- place them in {BIN_DIR} "
             f"or set {ENV_BIN_DIR}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())