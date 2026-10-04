@echo off
REM Stand-in launcher so subprocess.run([NORMALIZER_BIN, in, out, thr, gain])
REM works on Windows exactly as the real C++ binary would.
python "%~dp0fake_normalizer.py" %*
exit /b %ERRORLEVEL%
