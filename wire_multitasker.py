"""Wire the multitasker + lip-sync cells into kaggle_worker.ipynb.

WHAT IT DOES
------------
1. Inserts the **multitasker cell** just before the
   "Execute Mazinger" cell. When ``TDUBBER_MULTITASKER``
   is set, it runs the three-thread pipelined worker
   (multitasker.py, shipped inside the input dataset)
   and writes output.mp4 + report.json itself.
2. Inserts the **lip-sync cell** after it. When
   ``TDUBBER_LIP_SYNC`` is set, it stops vLLM (freeing
   GPU 0) and runs the post-dub mouth re-render
   (MuseTalk default, Wav2Lip fallback), overlapping
   GPU sync with Telegram upload.
3. Wraps the legacy "Execute Mazinger" cell in
   ``if not MULTITASKER_RAN:`` so the single-file path
   stays as the fallback -- old logic is never deleted,
   only bypassed when the pipelined path produced the dub.

Idempotent: running it twice changes nothing. Run from
the repo root:

    python wire_multitasker.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTEBOOK = ROOT / "kaggle_worker.ipynb"

MAZINGER_PREFIX = "# Execute Mazinger against the local Homura vLLM server."
MT_MARKER = "# Multitasker (TDUBBER_MULTITASKER=1)"
LS_MARKER = "# Lip-sync phase (TDUBBER_LIP_SYNC=1)"
GUARD_MARKER = "# Multitasker bypass"

MULTITASKER_CELL = f'''{MT_MARKER} -- the pipelined run.
#
# WHY THIS EXISTS
# ---------------
# The single-file cell below dubs one video end to end: ffmpeg,
# then GPU, then upload, each waiting for the previous. On Kaggle
# the GPU and the network are separate hardware, so this cell
# overlaps them with three threads and two bounded queues:
#
#   downloader (ffmpeg staging) --> gpu (mazinger) --> uploader (tgup)
#
# While the GPU dubs job N, the downloader extracts job N+1's
# voice reference and the uploader ships job N-1's finished dub
# to Telegram. With a dub_batch.json in the input dataset the
# whole batch pipelines; with one video, the voice-reference
# extraction still overlaps the first Mazinger stages.
#
# WHERE THE CODE COMES FROM
# -------------------------
# pipeline.py copies multitasker.py (and huggingface/) into the
# input dataset next to source_video.mp4, so the worker imports
# it straight from /kaggle/input -- no pip install, no extra
# dataset, no pack change.
import importlib.util
import json
import os
from pathlib import Path

MULTITASKER_RAN = False
if os.environ.get("TDUBBER_MULTITASKER", "").strip().lower() in ("1", "true", "yes", "on"):
    mt_hits = sorted(Path(input_dir).rglob("multitasker.py"))
    if not mt_hits:
        print("TDUBBER_MULTITASKER=1 but multitasker.py is not in the "
              "input dataset; falling back to the single-file cell below.",
              flush=True)
    else:
        spec = importlib.util.spec_from_file_location("multitasker", mt_hits[0])
        multitasker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(multitasker)

        jobs = multitasker.discover_jobs(input_dir=Path(input_dir))
        summary = multitasker.run_multitasker(
            jobs=jobs,
            workdir=Path("/kaggle/working"),
            input_dir=Path(input_dir),
            base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
            model_name=os.environ.get("OPENAI_MODEL", ""),
        )
        # Report in the legacy schema so the Kaggle download step
        # and the dashboard keep working unchanged.
        validations = summary.get("validations") or {{}}
        last_validation = (validations[sorted(validations)[-1]]
                             if validations else {{"passed": False, "checks": []}})
        report_data = {{
            "Status": "Success" if summary["failed"] == 0 else "Failed",
            "Target_Language": target_language,
            "Output_File": "output.mp4" if summary["uploaded"] else "Missing",
            "WER": "Not measured",
            "Sync_Offset": "Not measured",
            "Speaker_Similarity": "Not measured",
            "Multitasker": {{k: summary[k] for k in
                              ("jobs", "prepared", "dubbed", "uploaded",
                               "failed", "elapsed_sec")}},
            "Validation": last_validation,
        }}
        with open("/kaggle/working/report.json", "w", encoding="utf-8") as handle:
            json.dump(report_data, handle, ensure_ascii=False, indent=2)
        if summary["failed"]:
            with open("/kaggle/working/error_log.txt", "w",
                      encoding="utf-8") as handle:
                handle.write(
                    "multitasker: %d/%d job(s) failed; see "
                    "multitasker_ledger.jsonl and per-job "
                    "mazinger_stderr.log\\n"
                    % (summary["failed"], summary["jobs"]))
            # Leave a clean runtime: the legacy cell's finally block
            # normally stops vLLM, but it is skipped on this path.
            vllm = globals().get("vllm_process")
            if vllm is not None and vllm.poll() is None:
                vllm.terminate()
            raise RuntimeError(
                "Multitasker run had %d failure(s); see the ledger and "
                "per-job logs under /kaggle/working/multitasker_jobs/."
                % summary["failed"])
        MULTITASKER_RAN = True
        print("Multitasker finished: %s" % summary, flush=True)
'''

LIP_SYNC_CELL = f'''{LS_MARKER} -- mouth re-render on the freed GPU.
#
# WHY A SEPARATE CELL
# -------------------
# Mazinger dubs the AUDIO; the speaker's mouth still moves in
# the source language. This phase re-renders the mouth region
# of the SOURCE video to match the DUBBED audio: MuseTalk by
# default (latent-space inpainting, ~4 GB VRAM, 30fps+, MIT),
# Wav2Lip as the torch-only fallback when mmcv/mmpose cannot
# build against a newer Kaggle torch.
#
# IT RUNS AFTER vLLM STOPS: the translation LLM and the sync
# model both want the GPU, and a dub never needs them at the
# same time. vLLM is terminated here, GPU 0 goes to the sync
# model, and while the GPU syncs job N the uploader ships
# job N-1's synced video to the Telegram cloud center.
#
# bbox_shift tunes mouth openness (wide-mouth languages such
# as Hindi: try +3..+7). Set TDUBBER_BBOX_SHIFT or put
# "bbox_shift" in dub_job.json / dub_batch.json.
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

LIP_SYNC_RAN = False
if (os.environ.get("TDUBBER_LIP_SYNC", "").strip().lower()
        in ("1", "true", "yes", "on")
        and globals().get("MULTITASKER_RAN", False)):
    eligible = [job for job in jobs if job.lip_sync]
    if not eligible:
        print("TDUBBER_LIP_SYNC=1 but no job is flagged for "
              "lip-sync; skipping the phase.", flush=True)
    else:
        # Free GPU 0 for the sync model (single-GPU boxes).
        vllm = globals().get("vllm_process")
        if vllm is not None and vllm.poll() is None:
            vllm.terminate()
            try:
                vllm.wait(timeout=30)
            except subprocess.TimeoutExpired:
                vllm.kill()
            print("vLLM stopped; GPU 0 handed to the lip-sync model.",
                  flush=True)

        ls_hits = sorted(Path(input_dir).rglob("lip_sync.py"))
        mt_hits = sorted(Path(input_dir).rglob("multitasker.py"))
        if not ls_hits or not mt_hits:
            print("lip_sync.py or multitasker.py missing from the "
                  "input dataset; cannot run the lip-sync phase.",
                  flush=True)
        else:
            spec = importlib.util.spec_from_file_location(
                "lip_sync", ls_hits[0])
            lip_sync = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lip_sync)

            mt_spec = importlib.util.spec_from_file_location(
                "multitasker", mt_hits[0])
            multitasker = importlib.util.module_from_spec(mt_spec)
            mt_spec.loader.exec_module(multitasker)

            workdir = Path("/kaggle/working")
            provider = lip_sync.build_provider(workdir / "lip_sync")
            phase = multitasker.run_lip_sync_phase(
                jobs, provider, workdir=workdir)
            # The synced video is now the deliverable; refresh the
            # report so the download step and dashboard see it.
            report_path = Path("/kaggle/working/report.json")
            report = {{}}
            if report_path.is_file():
                report = json.loads(report_path.read_text(
                    encoding="utf-8"))
            report["LipSync"] = phase
            report["Validation"]["passed"] = (
                report.get("Validation", {{}}).get("passed", False)
                and phase["failed"] == 0)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            if phase["failed"]:
                raise RuntimeError(
                    "Lip-sync phase had %d failure(s); see "
                    "lip_sync_summary.json and the ledger."
                    % phase["failed"])
            LIP_SYNC_RAN = True
            print("Lip-sync phase finished: %s" % phase, flush=True)
'''

GUARD_LINES = [
    f"{GUARD_MARKER} (TDUBBER_MULTITASKER=1): the pipelined worker\n",
    "# cell above already ran Mazinger (and optionally the lip-sync\n",
    "# phase) with download/upload overlap and wrote\n",
    "# /kaggle/working/output.mp4 + report.json. Nothing left here.\n",
    "if globals().get(\"MULTITASKER_RAN\", False):\n",
    "    print(\"Multitasker already produced the dub -- "
    "single-file path skipped.\", flush=True)\n",
    "else:\n",
]


def source_text(cell: dict) -> str:
    return "".join(cell.get("source") or [])


def to_source_lines(text: str) -> list[str]:
    """nbformat stores source as a list of lines, each but the
    last ending in a newline."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return [line + "\n" for line in lines[:-1]] + [lines[-1]]


def main() -> int:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    cells = notebook["cells"]

    has_mt = any(source_text(cell).lstrip().startswith(MT_MARKER)
                   for cell in cells)
    has_ls = any(source_text(cell).lstrip().startswith(LS_MARKER)
                   for cell in cells)
    if has_mt and has_ls:
        print("multitasker + lip-sync cells already present; "
              "nothing to do.")
        return 0

    mazinger_index = next(
        (index for index, cell in enumerate(cells)
         if source_text(cell).lstrip().startswith(MAZINGER_PREFIX)),
        None,
    )
    if mazinger_index is None:
        raise SystemExit(f"could not find the Mazinger cell in {NOTEBOOK}")

    # Insert both cells before the Mazinger cell (lip-sync last,
    # so it runs after the multitasker cell).
    if not has_mt:
        cells.insert(mazinger_index, {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": to_source_lines(MULTITASKER_CELL),
        })
        mazinger_index += 1
    if not has_ls:
        cells.insert(mazinger_index, {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {},
            "outputs": [],
            "source": to_source_lines(LIP_SYNC_CELL),
        })
        mazinger_index += 1

    # Bypass the legacy cell when the pipelined path ran.
    mazinger_cell = cells[mazinger_index]
    original = mazinger_cell.get("source") or []
    if not source_text(mazinger_cell).lstrip().startswith(GUARD_MARKER):
        guarded = list(GUARD_LINES)
        for line in original:
            if line.strip():
                guarded.append("    " + line)
            else:
                guarded.append(line)
        mazinger_cell["source"] = guarded

    NOTEBOOK.write_text(
        json.dumps(notebook, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wired: multitasker cell + lip-sync cell inserted "
          f"before index {mazinger_index}; Mazinger cell guarded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
