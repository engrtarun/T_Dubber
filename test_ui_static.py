"""
Contract tests for the browser-side UI (ui_static/).

The Puter assistant, the clock, the job timer and the settings drawer are all
plain JavaScript, which python's other test files cannot execute. What they
CAN check is everything that breaks silently in a browser:

  * an asset missing or empty (the page then renders with no behaviour at all)
  * the script asking for a DOM id that no HTML defines (dead buttons)
  * a persona listed in JS but not in the Gradio radio, or vice versa
  * the regressions this file was written for: the 1-second busy loop, and
    innerHTML built out of log-derived text

Run with:  python test_ui_static.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ui_static

REPO = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(REPO, "ui_static")

HTML_FILES = ("topbar.html", "assistant.html")
EXPECTED_PERSONAS = ["Funny", "Serious", "Roast"]


def _read(name):
    with open(os.path.join(ASSET_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _app_py():
    with open(os.path.join(REPO, "app.py"), encoding="utf-8") as handle:
        return handle.read()


def _html_ids():
    ids = set()
    for name in HTML_FILES:
        ids.update(re.findall(r'id="([^"]+)"', _read(name)))
    ids.update(re.findall(r'elem_id="([^"]+)"', _app_py()))
    return ids


def _js_references(script):
    refs = set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', script))
    refs.update(re.findall(r"querySelector\(\s*[\"']#([A-Za-z0-9_-]+)", script))
    refs.update(re.findall(r"querySelectorAll\(\s*[\"']#([A-Za-z0-9_-]+)", script))
    return refs


def test_assets_exist_and_are_loaded():
    print("\n[1] assets: files exist, are non-empty, and reach the app")
    for name in HTML_FILES + ("app.js",):
        path = os.path.join(ASSET_DIR, name)
        assert os.path.isfile(path), f"missing asset: ui_static/{name}"
        assert os.path.getsize(path) > 0, f"empty asset: ui_static/{name}"

    assert ui_static.TOP_BAR_HTML, "TOP_BAR_HTML did not load"
    assert ui_static.PUTER_AI_HTML, "PUTER_AI_HTML did not load"
    assert ui_static.APP_JS, "APP_JS did not load"
    assert ui_static.TOP_BAR_HTML == _read("topbar.html")
    assert ui_static.APP_JS == _read("app.js")

    app_py = _app_py()
    assert "from ui_static import" in app_py, "app.py no longer imports the assets"
    assert "demo.load(js=APP_JS)" in app_py, "the script is not attached to demo.load"
    print(f"    topbar {len(ui_static.TOP_BAR_HTML)} B, assistant "
          f"{len(ui_static.PUTER_AI_HTML)} B, js {len(ui_static.APP_JS)} B")


def test_js_only_asks_for_ids_that_exist():
    print("\n[2] wiring: every id the script touches is defined somewhere")
    defined = _html_ids()
    missing = sorted(_js_references(ui_static.APP_JS) - defined)
    assert not missing, f"script references undefined ids: {missing}"
    print(f"    {len(defined)} ids defined, {len(_js_references(ui_static.APP_JS))} referenced")


def test_personas_match_the_gradio_radio():
    print("\n[3] personas: JS prompt map == Gradio radio choices")
    radio = re.search(
        r"gr\.Radio\((?P<args>[^)]*?puter_mode_selector[^)]*?)\)", _app_py(), re.S
    )
    assert radio, "the persona radio (elem_id=puter_mode_selector) is gone"
    choices_raw = re.search(r"choices=\[(?P<items>[^\]]*)\]", radio.group("args")).group("items")
    choices = [c.strip().strip('"\'') for c in choices_raw.split(",")]
    assert choices == EXPECTED_PERSONAS, f"radio choices are {choices}"

    js = ui_static.APP_JS
    for name in EXPECTED_PERSONAS:
        assert re.search(r"^\s*" + name + r":", js, re.M), f"persona {name} has no prompt in JS"
    for name in EXPECTED_PERSONAS:
        assert f'value="{name}"' in ui_static.TOP_BAR_HTML, f"settings have no {name} option"
    # The default persona must be one of them, or the assistant silently
    # falls back to Funny on every load.
    default = re.search(r"persona:\s*\"(\w+)\"", js).group(1)
    assert default in EXPECTED_PERSONAS, f"DEFAULTS.persona={default!r} is not a persona"
    print(f"    radio={choices}, js default={default}")


def test_no_busy_loop_and_single_ticker():
    print("\n[4] scheduler: one ticker, and unchanged logs reschedule instead of spinning")
    js = ui_static.APP_JS
    intervals = re.findall(r"setInterval\(", js)
    assert len(intervals) == 1, f"expected exactly one setInterval, found {len(intervals)}"

    # A dedup hit must push the next attempt out - the old code returned
    # early and left countdown at 0, which re-fired every single second.
    dedup = re.search(r"if \(!force && key === lastKey\) \{ ([^}]*) \}", js)
    assert dedup, "dedupe branch not found in insight()"
    assert "schedule" in dedup.group(1), (
        "an unchanged log must reschedule, got: " + dedup.group(1)
    )
    assert "clearInterval(window.td_tick_interval)" in js, "re-load would stack tickers"
    print("    1 ticker, dedupe reschedules, previous ticker cleared")


def test_history_and_message_are_not_html():
    print("\n[5] injection: log-derived text never becomes markup")
    js = ui_static.APP_JS
    assert "innerHTML" not in js, "innerHTML is used - history would be an XSS hole"
    assert "document.createTextNode" in js, "history/messages should be text nodes"
    # Button handlers are assigned (idempotent) rather than added, so a page
    # re-run cannot stack two handlers on one button. The single permitted
    # addEventListener is the OS-theme watcher, which removes itself first.
    listeners = re.findall(r"(\w+)\.addEventListener\(", js)
    assert all(name == "scheme" for name in listeners), (
        f"unexpected addEventListener on: {listeners}"
    )
    assert "removeEventListener" in js, "the theme watcher would stack on re-load"
    print("    textContent/createTextNode only, handlers assigned not added")


def test_settings_round_trip():
    print("\n[6] settings: defaults, storage key and controls all line up")
    js = ui_static.APP_JS
    topbar = ui_static.TOP_BAR_HTML

    key = re.search(r'const SET_KEY = "([^"]+)"', js)
    assert key, "no SET_KEY in the script"
    assert "localStorage.setItem" in js and "localStorage.getItem" in js

    for control in ("td-set-theme", "td-set-accent", "td-set-scale", "td-set-clock",
                    "td-set-refresh", "td-set-persona", "td-settings-reset"):
        assert f'id="{control}"' in topbar, f"settings panel is missing {control}"
        assert f'"{control}"' in js, f"the script never wires {control}"

    # The clock and the job timer are the two things asked for by name.
    for element in ("td-clock-time", "td-clock-date", "td-job-time", "td-job-note"):
        assert f'id="{element}"' in topbar, f"top bar is missing {element}"
    assert "toLocaleDateString" in js, "no date formatting in the clock"
    print(f"    storage key {key.group(1)}, clock + job timer elements present")


def main():
    tests = [
        test_assets_exist_and_are_loaded,
        test_js_only_asks_for_ids_that_exist,
        test_personas_match_the_gradio_radio,
        test_no_busy_loop_and_single_ticker,
        test_history_and_message_are_not_html,
        test_settings_round_trip,
    ]
    failures = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append((test.__name__, str(exc)))
            print(f"    FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, repr(exc)))
            print(f"    ERROR: {exc!r}")

    print("\n" + "=" * 62)
    if failures:
        for name, message in failures:
            print(f"FAILED  {name}: {message}")
        print(f"{len(failures)}/{len(tests)} tests failed")
        return 1
    print(f"All {len(tests)} UI asset tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
