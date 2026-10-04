"""Tests for channel selection, captions and tags.

These are the parts that decide whether a Telegram channel stays navigable or
turns into a wall of identical rows, so they are worth pinning down.

Run with:  python test_channel_caption.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
import link_resolver
from link_resolver import ResolvedMedia


def _use_temp_channels(path):
    original = app.CHANNELS_FILE
    app.CHANNELS_FILE = path
    app._ROUND_ROBIN_STATE["index"] = 0
    return original


def _safe(text):
    """Windows consoles are cp1252; keep test output printable."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def test_caption_carries_identity(scratch):
    print("\n[1] captions: a row in the channel identifies its own media")
    media = ResolvedMedia(
        path="C:/tmp/Lucifer.S01E13.mkv",
        kind="web",
        url="https://youtu.be/abc",
        page_url="https://www.youtube.com/watch?v=abc",
        title="Lucifer S01E13",
        size_bytes=9_955_747_524,
        duration=2640.0,
        extractor="Youtube",
        uploader="Netflix",
        height=1080,
    )
    caption = link_resolver.build_caption(media, kind="source")
    assert len(caption) <= 1024, len(caption)
    assert "Lucifer S01E13" in caption
    assert "Netflix" in caption
    assert "44m 0s" in caption, caption
    assert "1080p" in caption
    assert "youtube.com/watch?v=abc" in caption
    assert "#T_Dubber" in caption
    assert "#youtube" in caption
    assert "#netflix" in caption
    assert "#44min" in caption
    print("    " + _safe(caption.replace("\n", " | ")[:150]))


def test_caption_drops_url_before_tags(scratch):
    print("\n[2] captions: a very long title keeps its tags, not its URL")
    media = ResolvedMedia(
        path="C:/tmp/x.mp4",
        kind="web",
        url="https://example.com/" + "a" * 900,
        page_url="https://example.com/" + "a" * 900,
        title="T" * 600,
        size_bytes=1234567890,
        duration=65.0,
        extractor="Vimeo",
        uploader="U" * 200,
        height=720,
    )
    caption = link_resolver.build_caption(media)
    assert len(caption) <= 1024, len(caption)
    assert "#T_Dubber" in caption, "tags are what make a row findable"
    assert "example.com" not in caption, "the URL should have been dropped"
    print(f"    {len(caption)} chars, tags kept, URL dropped")


def test_tags_are_deduplicated(scratch):
    print("\n[3] tags: no duplicates, order preserved")
    media = ResolvedMedia(
        path="x.mp4", kind="web", url="", title="t", size_bytes=1,
        duration=3600.0, extractor="Youtube", uploader="YouTube", height=1080,
    )
    tags = link_resolver.build_tags(media)
    lowered = [t.lower() for t in tags]
    assert len(lowered) == len(set(lowered)), tags
    assert tags[0] == "T_Dubber"
    assert "1h00m" in tags, tags
    print("    " + " ".join("#" + t for t in tags))


def test_round_robin_spreads(scratch):
    print("\n[4] channels: round robin alternates across the community")
    path = os.path.join(scratch, "channels.json")
    original = _use_temp_channels(path)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "channels": ["@tgwebcloud1", "@tgwebcloud2", "@tgwebcloud3"],
                "strategy": "round_robin",
                "default_index": 0,
            }, handle)
        picked = [app.pick_channel() for _ in range(7)]
        assert picked[:3] == ["@tgwebcloud1", "@tgwebcloud2", "@tgwebcloud3"], picked
        assert picked[3] == "@tgwebcloud1", picked
        assert set(picked) == {"@tgwebcloud1", "@tgwebcloud2", "@tgwebcloud3"}
        print("    " + " -> ".join(picked))
    finally:
        app.CHANNELS_FILE = original


def test_single_channel_is_never_rotated(scratch):
    print("\n[5] channels: one channel keeps receiving everything")
    path = os.path.join(scratch, "channels1.json")
    original = _use_temp_channels(path)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"channels": ["@onlyone"], "strategy": "round_robin"}, handle)
        assert {app.pick_channel() for _ in range(4)} == {"@onlyone"}
        print("    always @onlyone")
    finally:
        app.CHANNELS_FILE = original


def test_fixed_and_least_used(scratch):
    print("\n[6] channels: fixed pins one, least_used fills the emptiest")
    path = os.path.join(scratch, "channels2.json")
    original = _use_temp_channels(path)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "channels": ["@archive", "@scratch1", "@scratch2"],
                "strategy": "fixed",
                "default_index": 0,
            }, handle)
        assert {app.pick_channel() for _ in range(3)} == {"@archive"}

        with open(path, "w", encoding="utf-8") as handle:
            json.dump({
                "channels": ["@archive", "@scratch1", "@scratch2"],
                "strategy": "least_used",
            }, handle)
        # The database has archives recorded against @tgwebcloud1/@tgwebcloud2,
        # so all three of these look equally empty and the tie breaks by name.
        chosen = app.pick_channel()
        assert chosen in ("@archive", "@scratch1", "@scratch2"), chosen
        print(f"    fixed -> @archive, least_used -> {chosen}")
    finally:
        app.CHANNELS_FILE = original


def test_save_channels_normalises(scratch):
    print("\n[7] channels: saving trims, dedupes and adds the @")
    path = os.path.join(scratch, "channels3.json")
    original = _use_temp_channels(path)
    try:
        message = app.save_channels(
            " tgwebcloud1 \n\n @tgwebcloud1\ntgwebcloud2  \n", "round_robin", 0
        )
        assert "2 channel(s)" in message, message
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved["channels"] == ["@tgwebcloud1", "@tgwebcloud2"], saved

        assert "at least one" in app.save_channels("   \n\n", "round_robin", 0)

        # An unknown strategy must not be persisted verbatim.
        app.save_channels("@a", "nonsense", 0)
        with open(path, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved["strategy"] == "round_robin", saved
        print("    trimmed, deduped, @ added, bad strategy rejected")
    finally:
        app.CHANNELS_FILE = original


def test_missing_channels_file_falls_back(scratch):
    print("\n[8] channels: a missing file never blocks an upload")
    path = os.path.join(scratch, "does_not_exist.json")
    original = _use_temp_channels(path)
    try:
        config = app.load_channels()
        assert isinstance(config["channels"], list)
        assert config["strategy"] in ("round_robin", "least_used", "random", "fixed")
        print(f"    fell back to {len(config['channels'])} configured channel(s)")
    finally:
        app.CHANNELS_FILE = original


def test_media_metadata_shape(scratch):
    print("\n[9] metadata: what the resolver hands the database")
    media = ResolvedMedia(
        path="x.mp4", kind="web", url="https://youtu.be/a",
        page_url="https://youtube.com/watch?v=a", title="T", size_bytes=99,
        duration=61.0, extractor="Youtube", uploader="U",
        thumbnail_url="https://i.ytimg.com/vi/a/hq.jpg", height=1080,
    )
    meta = media.to_metadata()
    for key in ("page_url", "extractor", "uploader", "title", "duration", "thumbnail"):
        assert key in meta, key
    assert media.resolution == "1080p"
    assert media.name == "x.mp4"
    print("    to_metadata() has every column the media_metadata table expects")


def main():
    tests = [
        test_caption_carries_identity,
        test_caption_drops_url_before_tags,
        test_tags_are_deduplicated,
        test_round_robin_spreads,
        test_single_channel_is_never_rotated,
        test_fixed_and_least_used,
        test_save_channels_normalises,
        test_missing_channels_file_falls_back,
        test_media_metadata_shape,
    ]

    original_channels = app.CHANNELS_FILE
    failures = []

    for test in tests:
        scratch = tempfile.mkdtemp(prefix="tchan_")
        try:
            test(scratch)
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print(f"    FAIL: {_safe(exc)}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, repr(exc)))
            print(f"    ERROR: {_safe(repr(exc))}")
        finally:
            app.CHANNELS_FILE = original_channels
            shutil.rmtree(scratch, ignore_errors=True)

    print("\n" + "=" * 62)
    if failures:
        for name, message in failures:
            print(f"FAILED  {name}: {_safe(message)}")
        print(f"{len(failures)}/{len(tests)} tests failed")
        return 1
    print(f"All {len(tests)} channel/caption tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
