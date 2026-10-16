"""``python -m infra`` -- read the manifest and tell the truth about it.

    python -m infra            # same as `plan`
    python -m infra plan       # compare the declared wiring with this machine
    python -m infra push       # stage + publish hop 3's session dataset

Exit status is 0 when every finding is informational and 1 when any is an
error, so this can sit in a pre-flight hook without anyone having to read the
output to know whether to continue.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from infra.manifest import (
    Manifest,
    check,
    default_manifest_path,
    load_manifest,
    render,
)
from infra.publish import build_dataset_dir, publish

__all__ = ["main"]


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _cmd_plan(manifest: Manifest, root: Path) -> int:
    findings = check(manifest, root)
    print(render(manifest, findings))
    return 1 if any(item.severity == "error" for item in findings) else 0


def _cmd_push(manifest: Manifest, root: Path, *, create_only: bool) -> int:
    staging = root / "infra" / "_staging"
    try:
        plan = build_dataset_dir(manifest, staging, root)
    except Exception as exc:  # noqa: BLE001 -- the message is the product here
        return _fail(str(exc))

    print(f"staged {plan.session_file.name} for {plan.slug} (private)")
    print(f"  auth key id {plan.key_id} (an id, not the key: sha1's last 8 bytes)")
    if create_only:
        print("  run the same command without --create once this succeeds")

    result = publish(plan, create_only=create_only)
    if not result.ok:
        return _fail(result.detail)
    print(result.detail)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit status."""
    parser = argparse.ArgumentParser(
        prog="python -m infra",
        description="T_Dubber runtime wiring, declared in HCL.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="plan",
        choices=["plan", "push"],
        help="plan (default) checks the wiring; push publishes hop 3",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="path to manifest.auto.tfvars (default: this repo's)",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="with push: create the dataset instead of versioning it",
    )
    args = parser.parse_args(argv)

    root = _root()
    path = Path(args.manifest) if args.manifest else default_manifest_path(root)
    try:
        manifest = load_manifest(path)
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim, it is actionable
        return _fail(str(exc))

    if args.command == "push":
        return _cmd_push(manifest, root, create_only=args.create)
    return _cmd_plan(manifest, root)


if __name__ == "__main__":
    raise SystemExit(main())
