"""Browser-side UI assets for the Gradio app, read from ``ui_static/``.

Why a separate module (and separate .html/.js files) instead of more triple
quoted strings inside ``app.py``:

  * app.py is already ~2500 lines and these are not Python - they are markup
    and client-side code that deserve to be readable, lintable and testable
    on their own.
  * JavaScript inside a Python string needs ``\\\\n`` for a newline and breaks
    on any ``\\u`` escape. As real files they are written the way the browser
    reads them.

Loading is deliberately tolerant: a missing file logs a warning and leaves the
constant empty rather than stopping the whole app from starting. The real
guard is ``test_ui_static.py``, which fails if any asset is missing, empty, or
refers to DOM ids that do not exist.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_static")


def _load(name: str) -> str:
    """Return an asset's text, or "" if it cannot be read."""
    path = os.path.join(ASSET_DIR, name)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:  # pragma: no cover - exercised only on a broken checkout
        logger.warning("UI asset %s could not be read: %s", name, exc)
        return ""


# Sticky top bar: brand, live clock + date, job timer, settings drawer.
TOP_BAR_HTML = _load("topbar.html")

# The Puter.js assistant card (markup only - no <script>, Gradio strips those).
PUTER_AI_HTML = _load("assistant.html")

# Everything with behaviour: settings, clock, job timer, assistant. Injected
# through demo.load(js=...), which is the one place Gradio still runs scripts.
APP_JS = _load("app.js")
