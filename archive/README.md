# 🗄️ archive/ — one-off scripts, yahan tak shift kiye gaye

Root saaf rakhne ke liye yahan rakha gaya hai. **Kuch delete nahi kiya gaya.**

> Project rule (`AI_MODELS_ROSTER.md`): *"purani files delete mat karo, bas bypass karo."*
> Inhone apna kaam kar liya, ab inhe root me rakhne se kisi ko benefit nahi,
> par delete se history/record udd jaati. Isliye — archive.

**Ye scripts chalana nahi hai.** Agar zaroorat pade toh path se explicitly call karo.

---

## `patch.py`, `patch2.py`
Notebook cell 0 ko "Mazinger local `/kaggle/input` se install karo" wala cell
banaya tha; `patch2.py` ne `"mazinger"` CLI ko `sys.executable -m mazinger`
me badla. **Applied — ab `kaggle_worker.ipynb` me seedha hai.**
`patch2.py` joined-then-resplit string surgery use karta tha — fragile.

## `fix_notebook.py` … `fix_notebook6.py` (7 files)
Ek ke baad ek **JSON surgery passes**:

| Script | Kya kiya |
|---|---|
| `fix_notebook.py` / `2` | `install_deps()` ka order badla (vLLM → torch → faster-whisper → speech) |
| `fix_notebook3.py` | cmd list me `--base-dir` insert |
| `fix_notebook4.py` | phir se `--base-dir` insert (alag anchor) |
| `fix_notebook5.py` | 4 ki **galat indentation** fix |
| `fix_notebook6.py` | `--gpu-memory-utilization` `0.72` → `0.65` (T4 headroom) |

⚠️ **3 aur 4 dono `--base-dir` insert karte hain.** Unhe sequence me chalaya to
**duplicate flag** ban jaata hai — `fix_notebook5.py` sirf 4 ke galti fix karne
ke liye bana. Teeno consecutive mistakes ka visible record. **Ab kabhi mat chalao.**

## `check_nb.py`, `check_nb2.py`, `check_nb3.py`
Notebook inspection scratch — `def install_deps()`, torch/vllm version lines
dhundhne ke liye. Saare hardcoded path use karte hain
`projects\Boyfriend_on_Demand___...\kernel_safe\kaggle_worker.ipynb` (per-project
copied notebook, source nahi).

## `test_chunkio.py` + `test_chunk.txt`
Scratchpad — `FileChunkIO` naam ka ek class, **zero assertions**. Module level pe
`test_chunk.txt` ko *rewrite* kar deta tha (isliye wo file 44 bytes ki thi).
Ye `telegram_uploader.HashingFileSlice` ka **origin prototype** hai — asli
implementation ab wahi hai, aur woh properly tested hai (`test_tg_cloud.py`).
Saath me apna fixture `test_chunk.txt` bhi yahin hai.

## `telegram_backup.py`
Original Bot-API stub — `send_message()` / `upload_video()` se ek video bhejna.
**Complete dead:** `telegram_uploader.py` (Telethon, chunked, resume, manifest)
ne ise poora replace kar diya. Koi nahi import karta.

Isi liye ye file **still referenced** tha (abhi update kiya):
* `requirements.txt` — `requests` ka wajah yahi tha
* `doctor.ps1` — `"requests" = "telegram_backup.py"` dependency attribution

`telegram_backup.py` ko archive karte waqt dono references ko `archive/` par
point kiya gaya. `requests` requirements.txt me **chhoda gaya** — wo bahut se
packages ka transitive dep hai, hataana risky hai.

---

## ⚠️ Ye wale P0 hain — archive se solve nahi hote

Neeche wali files **isi folder me bhi hain** (root se hatayi gayi hain taaki
koi by mistake na chalaye) par unka asli fix alag hai:

| File | Asli problem | Zaroori fix |
|---|---|---|
| `setup_config.py` | **plaintext `api_id` + `api_hash` + phone git me committed** | api_hash **rotate** karo (my.telegram.org → naya app) + git history purge |
| `scratch_login.py` | wahi creds **+ ek login OTP code** | same |

`config.json` me `api_hash` **DPAPI-encrypted** hai — yahi poora security point
hai. Ye do file uske saamne wo value **plaintext** me rakhti hain, isliye
encryption ka fayda zero ho jaata hai. Archive move karne se kuch nahi badla:
**git history me abhi bhi hai.**

Bade kaam ka reference ke liye `tg_API.jpg` bhi repo me committed hai
(my.telegram.org ka screenshot, `api_id`/`api_hash` padhne layak) — usse bhi
hatana chahiye.

---

## Artefacts (is folder me nahi — root par hi hain)

Ye abhi root par pade hain, koi code nahi padhta:

| File | Kya hai | Suggestion |
|---|---|---|
| `dm4.obj` | orphaned MSVC COFF object — SIMD-vs-scalar normalizer comparison harness. **Untracked**, koi build nahi karta. | git se bahar hi rakho, ya delete |
| `taruntest.txt` | 1 byte, content `"0"`. Koi nahi padhta. | delete |
| `dummy.mp4` | 0 bytes. `.dockerignore` me explicit naam hai. | delete |
| `dst/` | empty directory. Git empty dir track nahi karta. | delete |
| `src/file.txt` | 1 byte `"a"`. Stub, koi reference nahi. | delete |
| `normalizer*.pdb` ×4 | **~28 MB** MSVC debug symbols, 4 alag experiments ke. `*.pdb` LFS rule nahi hai isliye raw git me. | git history se hatao |
