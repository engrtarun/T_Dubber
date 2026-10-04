#!/bin/sh
# Stand-in launcher for the Rust `stitcher` binary (Linux/macOS).
exec python3 "$(dirname "$0")/fake_stitcher.py" "$@"
