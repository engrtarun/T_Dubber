@echo off
REM Stand-in launcher so subprocess.run([STITCHER_BIN, timeline.json]) works
REM on Windows exactly as the real Rust binary would.
python "%~dp0fake_stitcher.py" %*
exit /b %ERRORLEVEL%
