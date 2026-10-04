#!/bin/sh
# Stand-in launcher for the C++ `normalizer` binary (Linux/macOS).
exec python3 "$(dirname "$0")/fake_normalizer.py" "$@"
