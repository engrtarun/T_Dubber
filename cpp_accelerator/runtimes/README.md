# runtimes/ — llama.cpp + whisper.cpp, without pip

Replaces `vllm serve` and `faster-whisper` with two small C++ binaries and no
Python dependencies at all.

## Why this exists

Kaggle run `test4_gotgVERSION` burned **1067 s of a 1258 s run** inside
`pip install vllm`, then crashed with:

```
ImportError: libcudart.so.13: cannot open shared object file
```

vLLM 0.26.0 is built against CUDA 13; the Kaggle image ships CUDA 12. 85 % of
the run, zero output. Everything here exists so that a run spends **seconds**
setting up instead of minutes, and fails loudly rather than silently.

| | vLLM | this |
|---|---|---|
| setup | 1067 s pip install, 9 GB | ~20 s download, ~50 MB |
| failure mode | `ImportError` after 18 min | one `RUNTIME_MISSING` line, immediately |
| ASR | faster-whisper (ctranslate2 wheel set) | `whisper-cli`, one binary |

## Measured

Real `llama-server` (b11539, CPU) + real `whisper-cli` (b5454) + real models,
on this machine:

```
LLM : 2.0 s from process spawn to first completion  (/v1/chat/completions)
ASR : 2.8 s for 5.4 s of speech, 2 segments, correct timings
```

The vLLM path it replaces took 916 s to reach the same point.

## Layout

```
runtimes/
  build_runtimes.py      fetch / verify / locate the binaries
  llamacpp_bridge.py     LlamaServer  -> an OpenAI-compatible base_url
  whispercpp_bridge.py   WhisperCppRunner -> {"text", "segments", "language"}
  test_runtimes.py       71 tests, no real binaries, no network
  bin/llama/             llama.cpp binaries + their DLLs
  bin/whisper/           whisper.cpp binaries + their DLLs
```

### `bin/llama/` and `bin/whisper/` are separate on purpose

llama.cpp and whisper.cpp ship **colliding runtime DLL names** (`ggml.dll`,
`llama.dll`, `ggml-base.dll`, `ggml-cpu-*.dll`) at different ABI versions.
Installed into one shared directory, whichever was extracted second wins and
the other binary dies at load time with `WinError 216` — *before `main()`*, so
with no log output at all. Each project gets its own directory; `resolve()`
searches both.

## Getting the binaries

```bash
python cpp_accelerator/runtimes/build_runtimes.py                 # fetch
python cpp_accelerator/runtimes/build_runtimes.py --offline        # verify only
python cpp_accelerator/runtimes/build_runtimes.py llama-server whisper-cli
```

Output is one machine-readable line per binary:

```
RUNTIME_OK      llama-server  .../bin/llama/llama-server.exe
RUNTIME_MISSING whisper-cli   -
RUNTIME_ERROR   llama-server  sha256 mismatch (got 5b92164b26a620b5)
```

Exit code is 0 only if every requested binary is present.

**Warm run = no-op.** If the file exists and its sha256 matches
`bin/<project>/MANIFEST.sha256`, nothing is downloaded, nothing is rewritten.
That is the whole point, so `ensure()` defaults to `offline=True`.

Hand-placed binaries are accepted as `RUNTIME_OK` with an `unpinned` note:
refusing them would make the resolver useless on Windows, where no upstream
Linux build exists and a local CMake build is the normal route.

### Resolution order

1. `$TDUBBER_BIN_DIR` (os.pathsep-separated) — point at a mounted Kaggle dataset
2. `runtimes/bin/` and its `llama/` + `whisper/` subdirectories
3. `cpp_accelerator/build*/` — a local CMake build
4. `PATH`, **canonical name only**

Step 4 deliberately skips aliases: Windows resolves `main` through `PATHEXT`
to `C:\Windows\System32\main.CPL`, a Control Panel applet. Aliasing it on PATH
"finds" a system file that is not a speech recogniser.

Aliases (`whisper-cpp`, `whisper-whisper-cli`, `main`) *are* probed inside
directories, since upstream renamed whisper's CLI twice.

## LLM: `llamacpp_bridge.py`

```python
import sys; sys.path.insert(0, "/kaggle/input/runtimes")
from llamacpp_bridge import LlamaServer, resolve, build_openai_client

with LlamaServer(resolve("llama-server"),
                 "IndexTeam/Index-Homura-2B-GGUF",   # or /kaggle/working/homura.gguf
                 ctx=16384, threads=os.cpu_count()) as srv:
    base_url, model = srv.base_url, srv.served_model_name
    # mazinger's existing path, unchanged:
    #   from mazinger.llm import build_client
    #   client = build_client(base_url=base_url, api_key="EMPTY")
```

`build_openai_client(server=srv)` returns a small `Endpoint` carrying
`.base_url` / `.api_key` / `.model` if you would rather construct the SDK
object yourself. `Endpoint.openai_client()` does that for you — the `openai`
import is lazy, so this module stays importable with no third-party packages.

### Exact argv

For a local `.gguf`:

```
llama-server -m /path/homura.gguf \
  --host 127.0.0.1 --port <port> \
  -c 16384 -np 1 -t <threads> \
  --alias homura
```

For a Hub id:

```
llama-server -hf IndexTeam/Index-Homura-2B-GGUF \
  --host 127.0.0.1 --port <port> \
  -c 16384 -np 1 -t <threads> \
  --alias IndexTeam/Index-Homura-2B-GGUF
```

Flag notes, each of which cost a run to learn:

* **`-m` vs `-hf`** — decided by `_looks_like_repo()`. Exists on disk → `-m`;
  `.gguf`/`.ggml`/`.bin` suffix → `-m`; drive letter or leading `/` → `-m`;
  otherwise `-hf`. Passing `-hf` for a local path makes llama.cpp try to fetch
  a repo called `C:/models/...`.
* **`-c 16384`, not 4096** — the pipeline asks for 8000 output tokens (resegment
  merge) and 4000+prompt (fit check). Both were rejected with
  `max_tokens cannot be greater than max_model_len`.
* **`--alias`** — the OpenAI client sends whatever `--llm-model` mazinger was
  given. Without an alias the server advertises a different name and every
  request 404s with "model not found" while the model sits loaded.
* **`--jinja` is NOT passed** — the GGUF's own chat template is the default,
  and passing it to a build that does not know the flag aborts the server
  before it binds a port.
* **`-np`** — parallel slots. 1 is right for a single dubbing stream.

### Behaviour that matters in a notebook

* `start()` polls `GET /v1/models` until HTTP 200. If the process dies first it
  raises immediately with the exit code; on timeout it raises
  `LlamaServerTimeout` (a `TimeoutError`).
* **Both exceptions carry the last 30 000 characters of the log**, mirroring
  the notebook cell that caught the vLLM failure. The useful line is at the end
  of the log, not the start.
* `port=0` is resolved to a real free port **before** launch. `--port 0` means
  "any port" to llama-server, which binds one it never reports back — every
  probe then 404s and the server looks dead while it is serving.
* A busy port is swapped for a free one, never reused.
* `stop()` kills the process group (`taskkill /T` on Windows, `killpg` on
  POSIX) because llama-server spawns worker children that would otherwise keep
  holding the port. Idempotent, and safe in `finally`.
* `__enter__`/`__exit__` are supported.

### GPU

The prebuilt CPU assets are what `build_runtimes.py` picks on purpose. A CUDA
13 build would reproduce the exact `libcudart.so.13` failure this migration
exists to remove. For GPU offload, build from source and drop the result in
`bin/llama/`, then pass `extra_args=["-ngl", "99"]`. On a Kaggle T4 a 2B model
on the CPU build is already fast enough for a single dubbing stream.

## ASR: `whispercpp_bridge.py`

```python
from whispercpp_bridge import WhisperCppRunner, resolve

runner = WhisperCppRunner(resolve("whisper-cli"), "ggml-base.bin",
                          language="hi", threads=os.cpu_count())
result = runner.transcribe_wav("stage1.wav")     # non-WAV is decoded first
result["text"], result["segments"], result["language"]
```

Returns exactly:

```python
{"text": str,
 "segments": [{"start": float, "end": float, "text": str}, ...],
 "language": str}
```

### CLI surface

```
whisper-cli -m <model> -f <wav> -oj -of <prefix> [-l <lang>] [-t <threads>]
           [--vad] [--vad-threshold <f>] [--max-len <n>]
```

Every one of these exists upstream. Optional flags are emitted only when asked
for — an unknown flag aborts the binary before it reads a sample. `-of` gets a
unique per-call prefix so concurrent jobs cannot clobber each other.

`transcribe_wav()` parses the `-oj` JSON first and falls back to the `.srt`,
which carries the same timings. JSON is read leniently: a top-level
`transcription` array (current builds), `segments` (some forks) or a bare list
are all accepted, and timestamps parse from `offsets` (milliseconds),
`timestamps` (SRT strings) or OpenAI-shaped `start`/`end` (seconds). The unit
is explicit at each call site because the two conventions differ by 1000x.

`convert_to_wav()` shells out to ffmpeg for anything that is not already a
16 kHz mono PCM16 WAV.

### It never returns an empty transcript silently

A missing output file, unparseable output, non-zero exit, or a genuinely empty
result all raise `WhisperCppError` carrying the stderr tail. This is the one
behaviour worth being strict about: an empty ASR result still produces a
two-hour dub of nothing, and every stage downstream reports success. Pass
`allow_empty=True` if silence really is the expected answer.

This is not theoretical — verified live: a 440 Hz sine tone fed to real
`whisper-cli` yields a well-formed transcript of zero characters, and the
bridge refused it.

## Tests

```bash
python cpp_accelerator/runtimes/test_runtimes.py            # unittest
python -m pytest cpp_accelerator/runtimes/test_runtimes.py -v
python -m unittest discover -s cpp_accelerator/runtimes
```

**71 tests, no real binaries and no network.** The fakes are real subprocesses,
not mocks: a `http.server` script standing in for `llama-server`, a script
writing real `.json`/`.srt` for `whisper-cli`, one that writes garbage to
stderr and exits non-zero. Mocking `subprocess.run` would have made the fake's
own argv parsing — the thing under test — the fake.

Six of these tests exist because they caught a real bug during development:

| Test | Bug it caught |
|---|---|
| `test_dll_companions_are_extracted_alongside_the_exe` | Windows build is a DLL farm; exe alone dies `STATUS_DLL_NOT_FOUND` |
| `test_port_zero_is_resolved_before_launch` | `--port 0` → server binds an unadvertised port |
| `test_canonical_name_beats_an_alias_that_sorts_first` | extracted deprecated `main.exe` as `whisper-cli.exe` |
| `test_windows_path_is_not_mistaken_for_a_repo` | `C:/x.gguf` sent to `-hf` |
| `test_path_lookup_never_resolves_an_alias` | resolved to `System32\main.CPL` |
| `test_binary_is_extracted_into_a_per_project_directory` | llama/whisper DLL collision |

## Known upstream quirk

whisper.cpp's Windows zip (b5454) ships `main.exe` and `whisper-cli.exe` that
print a deprecation warning and exit 1 when the newer `whisper-whisper-cli.exe`
is absent. Extraction prefers the canonical name over aliases precisely so the
working binary is the one installed. If a future release breaks this, the
bridge reports it loudly:

```
WhisperCppError: whisper-cli failed on speech16.wav (exit 1)
--- stderr tail ---
WARNING: The binary 'whisper-cli.exe' is deprecated.
```