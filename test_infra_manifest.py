"""Tests for infra/ -- the HCL manifest and the three Telegram hops.

Runs both ways, as every test in this repo must (CODE_MAP.md 5.2):

    python test_infra_manifest.py
    python -m pytest test_infra_manifest.py

No test here touches the real ``tgup.session``, the real Kaggle dataset, or the
real CLI. Every session file is synthesised into a scratch workspace, and the
one subprocess in ``infra.publish`` is replaced by a capturing runner, so this
file is safe to run on a machine that has a live Telegram login.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
import unittest
from pathlib import Path
from typing import TYPE_CHECKING

import testkit
from infra.hcl import HclError, load_file, loads
from infra.manifest import (
    check,
    load_manifest,
    read_key_id,
    render,
    resolve_hop_path,
)
from infra.publish import build_dataset_dir, publish

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    # The shape infra.publish.publish() asks for: argv in, (code, output) out.
    Capture = Callable[[Sequence[str]], "tuple[int, str]"]

# A deterministic 256-byte "auth key". An MTProto auth key is exactly 256 bytes,
# and both readers in the repo refuse anything else, so the fixture has to be
# that size or the tests would be asserting on a rejection path by accident.
FAKE_KEY = b"\xa7" * 256

# sha1 here is not a security choice, it is a protocol requirement: an MTProto
# auth key id is defined as sha1(auth_key)[-8:], and gotd checks exactly those
# bytes. telethon_session.py encodes the same rule, and a fixture that used a
# different digest would stop testing the thing it claims to test.
FAKE_KEY_ID = base64.b64encode(
    hashlib.sha1(FAKE_KEY).digest()[-8:]  # noqa: S324 -- MTProto key-id rule
).decode("ascii")

MANIFEST_HCL = """
# a comment
telegram = {
  channel          = "@example_channel"
  telethon_session = "telethon.session"
  local_session    = "tgup.session"
}

kaggle = {
  dataset_slug = "someone/tdubber-session"
  private      = true
  session_path = "/kaggle/working/tgup.session"
}

edge = {
  url = "https://example.hf.space"
}
"""


def write_gotd(path: Path, key: bytes = FAKE_KEY) -> Path:
    """A gotd session document, shaped exactly as telethon_session writes one."""
    document = {
        "Version": 1,
        "Data": {
            "Config": {},
            "DC": 5,
            "Addr": "91.108.56.185:443",
            "AuthKey": base64.b64encode(key).decode("ascii"),
            "AuthKeyID": base64.b64encode(
                hashlib.sha1(key).digest()[-8:]  # noqa: S324 -- key-id rule
            ).decode("ascii"),
            "Salt": 0,
        },
    }
    return testkit.write_json(path, document)


def write_telethon(path: Path, key: bytes = FAKE_KEY) -> Path:
    """A Telethon SQLite session holding the same key."""
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "create table sessions (dc_id integer, server_address text, "
            "port integer, auth_key blob)"
        )
        con.execute(
            "insert into sessions values (?, ?, ?, ?)",
            (5, "91.108.56.185", 443, key),
        )
        con.commit()
    finally:
        con.close()
    return path


def write_manifest(root: Path, text: str = MANIFEST_HCL) -> Path:
    path = root / "manifest.auto.tfvars"
    path.write_text(text, encoding="utf-8")
    return path


class HclReaderTest(unittest.TestCase):
    """infra/hcl.py -- the subset, and the refusals."""

    def test_parses_attributes_and_nested_objects(self) -> None:
        doc = loads('a = 1\nb = "two"\nc = true\nd = null\n')
        self.assertEqual(
            doc.body.attributes,
            {"a": 1, "b": "two", "c": True, "d": None},
        )

    def test_parses_list_and_object(self) -> None:
        doc = loads('items = ["x", "y"]\nmap = { k = 1, nested = { deep = "v" } }\n')
        self.assertEqual(doc.body.attributes["items"], ["x", "y"])
        self.assertEqual(
            doc.body.attributes["map"],
            {"k": 1, "nested": {"deep": "v"}},
        )

    def test_blocks_keep_labels_and_body(self) -> None:
        doc = loads('variable "channel" {\n  default = "x"\n}\n')
        self.assertEqual(len(doc.body.blocks), 1)
        block = doc.body.blocks[0]
        self.assertEqual(block.type, "variable")
        self.assertEqual(block.labels, ("channel",))
        self.assertEqual(block.body.attributes, {"default": "x"})

    def test_comments_are_ignored(self) -> None:
        doc = loads("# hash\n// slash\n/* block */\na = 1 # trailing\n")
        self.assertEqual(doc.body.attributes, {"a": 1})

    def test_dashes_are_allowed_in_identifiers(self) -> None:
        doc = loads("my-key = 1\n")
        self.assertEqual(doc.body.attributes, {"my-key": 1})

    def test_escapes_are_decoded(self) -> None:
        doc = loads(r'path = "a\tb\\c\"d"' + "\n")
        self.assertEqual(doc.body.attributes["path"], 'a\tb\\c"d')

    def test_refuses_interpolation(self) -> None:
        with self.assertRaises(HclError) as caught:
            loads('a = "${var.b}"\n')
        self.assertIn("interpolation", str(caught.exception))

    def test_refuses_interpolation_inside_a_string(self) -> None:
        with self.assertRaises(HclError):
            loads('a = "x ${y}"\n')

    def test_refuses_unterminated_string(self) -> None:
        with self.assertRaises(HclError):
            loads('a = "never closed\n')

    def test_refuses_unterminated_block_comment(self) -> None:
        with self.assertRaises(HclError):
            loads("/* forever\n")

    def test_error_names_the_line(self) -> None:
        with self.assertRaises(HclError) as caught:
            loads("a = 1\nb = ${oops}\n")
        self.assertEqual(caught.exception.line, 2)

    def test_refuses_a_block_inside_an_object_literal(self) -> None:
        with self.assertRaises(HclError):
            loads('a = { b "label" { c = 1 } }\n')

    def test_refuses_a_terraform_type_expression(self) -> None:
        # `type = object({ ... })` is terraform, not manifest. manifest.auto
        # .tfvars is deliberately the only file infra/ reads, so the reader does
        # not grow to understand expressions it has no use for -- variables.tf
        # and outputs.tf stay terraform's business.
        with self.assertRaises(HclError):
            loads("type = object({ a = string })\n")

    def test_reads_the_shipped_versions_file(self) -> None:
        # versions.tf is blocks-only, which the reader does handle. Checked so
        # that a future parser change cannot quietly break the one .tf file that
        # has no expressions in it.
        doc = load_file(Path(__file__).resolve().parent / "infra" / "versions.tf")
        self.assertEqual([block.type for block in doc.body.blocks], ["terraform"])


class ManifestLoadTest(unittest.TestCase):
    """load_manifest -- what the file has to contain to be usable at all."""

    def setUp(self) -> None:
        self.ws = testkit.ScratchWorkspace()
        self.addCleanup(self.ws.close)

    def test_loads_the_three_hops(self) -> None:
        manifest = load_manifest(write_manifest(self.ws.path))
        self.assertEqual(
            [hop.name for hop in manifest.hops],
            ["telethon", "local_gotd", "kaggle"],
        )
        self.assertEqual(manifest.channel, "@example_channel")
        self.assertEqual(manifest.kaggle_dataset, "someone/tdubber-session")
        self.assertTrue(manifest.kaggle_private)

    def test_hop_formats_are_telethon_then_gotd(self) -> None:
        manifest = load_manifest(write_manifest(self.ws.path))
        formats = {hop.name: hop.session_format for hop in manifest.hops}
        self.assertEqual(
            formats,
            {"telethon": "telethon", "local_gotd": "gotd", "kaggle": "gotd"},
        )

    def test_missing_file_is_an_error_not_an_empty_manifest(self) -> None:
        # An empty manifest would make check() report a clean machine and prove
        # nothing, which is the one outcome worse than a red check.
        with self.assertRaises(HclError):
            load_manifest(self.ws.path / "nope.auto.tfvars")

    def test_a_hop_without_a_path_is_refused(self) -> None:
        text = MANIFEST_HCL.replace('local_session    = "tgup.session"', 'local_session    = ""')
        with self.assertRaises(HclError) as caught:
            load_manifest(write_manifest(Path(self.ws.path), text))
        self.assertIn("local_gotd", str(caught.exception))

    def test_unknown_hop_name_raises(self) -> None:
        manifest = load_manifest(write_manifest(self.ws.path))
        with self.assertRaises(KeyError):
            manifest.hop("nope")

    def test_real_repo_manifest_parses(self) -> None:
        # The shipped manifest must stay loadable, or the tool is decoration.
        manifest = load_manifest()
        self.assertEqual(len(manifest.hops), 3)


class KeyIdTest(unittest.TestCase):
    """read_key_id / key_id_of -- total, and comparable across formats."""

    def setUp(self) -> None:
        self.ws = testkit.ScratchWorkspace()
        self.addCleanup(self.ws.close)
        self.root = self.ws.path
        self.manifest = load_manifest(write_manifest(self.root))

    def test_missing_file_gives_none(self) -> None:
        self.assertIsNone(read_key_id(self.root / "absent.session"))

    def test_binary_file_gives_none_and_does_not_raise(self) -> None:
        # This is the bug the first smoke test found: a Telethon session is
        # SQLite, not UTF-8, and the decode error used to escape as a traceback.
        path = write_telethon(self.root / "telethon.session")
        self.assertIsNone(read_key_id(path))

    def test_garbage_gives_none(self) -> None:
        path = self.root / "garbage.session"
        path.write_bytes(b"{not json at all")
        self.assertIsNone(read_key_id(path))

    def test_telethon_and_gotd_yield_the_same_key_id(self) -> None:
        # The point of the whole comparison: one is SQLite and one is JSON, and
        # both must describe the same 256 bytes.
        telethon = write_telethon(self.root / "telethon.session")
        gotd = write_gotd(self.root / "tgup.session")
        hop_t = self.manifest.hop("telethon")
        hop_g = self.manifest.hop("local_gotd")

        from infra.manifest import key_id_of

        self.assertEqual(key_id_of(hop_t, telethon), FAKE_KEY_ID)
        self.assertEqual(key_id_of(hop_g, gotd), FAKE_KEY_ID)
        self.assertEqual(
            key_id_of(hop_t, telethon),
            key_id_of(hop_g, gotd),
        )

    def test_a_different_key_is_reported_as_different(self) -> None:
        other = write_gotd(self.root / "other.session", key=b"\x5c" * 256)
        self.assertNotEqual(read_key_id(other), FAKE_KEY_ID)


class ResolveAndCheckTest(unittest.TestCase):
    """resolve_hop_path and check -- the divergence finding is the product."""

    def setUp(self) -> None:
        self.ws = testkit.ScratchWorkspace()
        self.addCleanup(self.ws.close)
        self.root = self.ws.path
        self.manifest = load_manifest(write_manifest(self.root))
        write_telethon(self.root / "telethon.session")
        write_gotd(self.root / "tgup.session")

    def _statuses(self, environ: dict[str, str]) -> dict[str, str]:
        findings = check(self.manifest, self.root, environ)
        return {item.hop: item.status for item in findings}

    def test_relative_declared_path_resolves_against_the_root(self) -> None:
        hop = self.manifest.hop("local_gotd")
        resolved = resolve_hop_path(self.manifest, hop, self.root, {})
        self.assertEqual(resolved, self.root / "tgup.session")

    def test_environment_beats_the_manifest(self) -> None:
        # Not an opinion: the Dockerfile sets TGUP_SESSION and tgup_bridge.py
        # honours it. A manifest whose env override is ignored would be a
        # manifest that lies with a straight face.
        hop = self.manifest.hop("local_gotd")
        override = str(self.root / "elsewhere.session")
        resolved = resolve_hop_path(self.manifest, hop, self.root, {"TGUP_SESSION": override})
        self.assertEqual(str(resolved), override)

    def test_off_means_no_override(self) -> None:
        hop = self.manifest.hop("telethon")
        resolved = resolve_hop_path(self.manifest, hop, self.root, {"TGUP_TELETHON_SESSION": "off"})
        self.assertEqual(resolved, self.root / "telethon.session")

    def test_matching_keys_are_ok(self) -> None:
        statuses = self._statuses({})
        self.assertEqual(statuses["telethon"], "ok")
        self.assertEqual(statuses["local_gotd"], "ok")

    def test_missing_hop_is_an_error_with_a_fix(self) -> None:
        (self.root / "tgup.session").unlink()
        findings = check(self.manifest, self.root, {})
        missing = [item for item in findings if item.hop == "local_gotd"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].status, "missing")
        self.assertEqual(missing[0].severity, "error")
        self.assertIn("telethon_session.py", missing[0].fix)

    def test_kaggle_hop_names_the_push_command(self) -> None:
        findings = check(self.manifest, self.root, {})
        kaggle = [item for item in findings if item.hop == "kaggle"]
        self.assertEqual(kaggle[0].status, "missing")
        self.assertIn("push", kaggle[0].fix)

    def test_override_disagreeing_with_the_manifest_is_reported(self) -> None:
        # Exactly the hop-3 bug: the path lives in a Dockerfile ENV and in
        # nothing else, so the declared wiring and the live wiring disagree.
        findings = check(
            self.manifest, self.root, {"TGUP_SESSION": r"C:\somewhere\else\tgup.session"}
        )
        diverges = [item for item in findings if item.status == "diverges"]
        self.assertTrue(diverges)
        self.assertEqual(diverges[0].severity, "error")

    def test_a_different_key_is_a_warn_not_an_error(self) -> None:
        write_gotd(self.root / "other.session", key=b"\x5c" * 256)
        env = {"TGUP_SESSION": str(self.root / "other.session")}
        findings = check(self.manifest, self.root, env)
        mismatch = [item for item in findings if item.status == "key_mismatch"]
        self.assertTrue(mismatch)
        self.assertEqual(mismatch[0].severity, "warn")

    def test_corrupt_session_is_reported_not_raised(self) -> None:
        (self.root / "tgup.session").write_text("{oops", encoding="utf-8")
        statuses = self._statuses({})
        self.assertEqual(statuses["local_gotd"], "corrupt")

    def test_public_dataset_is_an_error(self) -> None:
        text = MANIFEST_HCL.replace("private      = true", "private      = false")
        manifest = load_manifest(write_manifest(self.root, text))
        findings = check(manifest, self.root, {})
        self.assertTrue(any(item.detail.startswith("kaggle.private is false") for item in findings))

    def test_render_puts_errors_first(self) -> None:
        text = render(self.manifest, check(self.manifest, self.root, {}))
        self.assertLess(text.index("[error]"), text.index("[info ]"))
        self.assertIn("@example_channel", text)

    def test_render_prints_the_key_id_but_never_the_key(self) -> None:
        # The id is eight bytes of the key's SHA-1, so it is an identifier and
        # showing it in full is both safe and the whole point of the report.
        # What must never appear is the 256-byte key itself.
        text = render(self.manifest, check(self.manifest, self.root, {}))
        self.assertIn(FAKE_KEY_ID, text)
        self.assertNotIn(
            base64.b64encode(FAKE_KEY).decode("ascii"),
            text,
        )
        self.assertNotIn(FAKE_KEY.hex(), text)


class PublishTest(unittest.TestCase):
    """build_dataset_dir / publish -- hop 3, and the gate in front of it."""

    def setUp(self) -> None:
        self.ws = testkit.ScratchWorkspace()
        self.addCleanup(self.ws.close)
        self.root = self.ws.path
        self.manifest = load_manifest(write_manifest(self.root))
        write_telethon(self.root / "telethon.session")
        write_gotd(self.root / "tgup.session")

    def _capture(self, code: int = 0, output: str = "") -> tuple[Capture, list[tuple[str, ...]]]:
        """A runner that records argv instead of running kaggle."""
        seen: list[tuple[str, ...]] = []

        def runner(argv: Sequence[str]) -> tuple[int, str]:
            seen.append(tuple(argv))
            return code, output

        return runner, seen

    def test_stages_the_session_and_its_metadata(self) -> None:
        staging = self.root / "_staging"
        plan = build_dataset_dir(self.manifest, staging, self.root, {})
        self.assertTrue(plan.session_file.is_file())
        self.assertEqual(plan.key_id, FAKE_KEY_ID)
        self.assertTrue(plan.is_private)
        self.assertEqual(
            sorted(p.name for p in plan.files()), ["dataset-metadata.json", "tgup.session"]
        )

    def test_stages_nothing_else(self) -> None:
        # The blast radius of this dataset leaking should be exactly what the
        # worker cannot run without.
        staging = self.root / "_staging"
        build_dataset_dir(self.manifest, staging, self.root, {})
        self.assertEqual(
            sorted(p.name for p in staging.iterdir()), ["dataset-metadata.json", "tgup.session"]
        )

    def test_metadata_marks_the_dataset_private(self) -> None:
        staging = self.root / "_staging"
        build_dataset_dir(self.manifest, staging, self.root, {})
        import json

        meta = json.loads((staging / "dataset-metadata.json").read_text())
        self.assertIs(meta["isPrivate"], True)
        self.assertEqual(meta["id"], "someone/tdubber-session")

    def test_refuses_a_public_dataset(self) -> None:
        text = MANIFEST_HCL.replace("private      = true", "private      = false")
        manifest = load_manifest(write_manifest(self.root, text))
        with self.assertRaises(HclError) as caught:
            build_dataset_dir(manifest, self.root / "_staging", self.root, {})
        self.assertIn("SECURITY.md", str(caught.exception))

    def test_refuses_an_empty_slug(self) -> None:
        text = MANIFEST_HCL.replace('dataset_slug = "someone/tdubber-session"', 'dataset_slug = ""')
        manifest = load_manifest(write_manifest(self.root, text))
        with self.assertRaises(HclError):
            build_dataset_dir(manifest, self.root / "_staging", self.root, {})

    def test_refuses_when_the_local_session_is_absent(self) -> None:
        (self.root / "tgup.session").unlink()
        with self.assertRaises(HclError) as caught:
            build_dataset_dir(self.manifest, self.root / "_staging", self.root, {})
        self.assertIn("telethon_session.py", str(caught.exception))

    def test_publish_uses_an_argv_list_and_never_a_shell(self) -> None:
        plan = build_dataset_dir(self.manifest, self.root / "_staging", self.root, {})
        runner, seen = self._capture()
        result = publish(plan, runner)
        self.assertTrue(result.ok)
        self.assertEqual(seen[0][0], "kaggle")
        self.assertIn("--private", seen[0])
        self.assertIn("version", seen[0])

    def test_create_is_available_for_the_first_push(self) -> None:
        plan = build_dataset_dir(self.manifest, self.root / "_staging", self.root, {})
        runner, seen = self._capture()
        publish(plan, runner, create_only=True)
        self.assertIn("create", seen[0])

    def test_a_failing_cli_is_reported_with_its_output(self) -> None:
        plan = build_dataset_dir(self.manifest, self.root / "_staging", self.root, {})
        runner, _ = self._capture(code=1, output="dataset already exists")
        result = publish(plan, runner)
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
