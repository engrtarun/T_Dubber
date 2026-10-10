# 🤗 huggingface/ -- T_Dubber's Hugging Face connector

Ye folder T_Dubber ka **single point of contact** hai Hugging Face ke
saath. Isme sab milega jo jodna aur use karne ke liye chahiye:

| File | Kaam |
|---|---|
| **`SETUP_HF.md`** | **Kaha kya karna hai -- setup steps, token, verify, troubleshoot. Yahan se shuru karo.** |
| `models.json` | **Torch-era model roster** -- kaunsa model, kaunsi role (LLM/ASR/TTS), kitne GB, gated ya nahi. |
| `models_gguf.json` | **GGUF roster** -- llama.cpp / whisper.cpp blobs, unke **verified sha256**, aur `ctx_tokens`. Go roster (`edge/model_gguf.go`) ka mirror hai; ek test dono ko field-by-field compare karta hai, toh digest drift nahi kar sakta. |
| `hf_store.py` | **Connector module** -- `ensure_model()` / `setup_env()` / `warm_status()`. Mounted cache → HF_HOME cache → edge Space → hub download, isi order me. `suffix=".gguf"` de kar .gguf/.bin bhi resolve kar sakta hai. |
| `gguf_store.py` | **GGUF resolver** -- `ensure_gguf()` / `warm_status()`. **Wohi precedence chain**, upar wala, kyunki torch stack ja raha hai. Digest ke baad verify, mismatch par file delete + raise. |
| `seed_cache.py` | **One-shot pre-pull** -- poore roster ko ek folder me download karke use Kaggle "cache dataset" banane ke liye (warm run = 0 download). |
| `test_hf_store.py` | **Regression tests** -- edge route, Python-version-safe unpack, dataset discovery, report/resolver agreement, `suffix` parameter, streaming digest. `python huggingface\test_hf_store.py` |
| `test_gguf_store.py` | **GGUF resolver tests** -- roster digest policy, `/gguf/` route, verify-then-delete, precedence order. `python huggingface\test_gguf_store.py` |
| `.env.example` | Env var template (`HF_TOKEN` wagera). Copy karke `.env` banao. |

## Funda

Kaggle worker ka 74% time setup me jaata hai (pip 378s + Homura-2B
download 525s). HF weights agar **mounted** hon (Kaggle input dataset
ya cache dataset), toh download 0s. `hf_store.py` exactly yahi
resolve karta hai:

```
1. /kaggle/input  (mounted pack/cache dataset)  -> 0 bytes
2. HF_HOME        (/kaggle/working/hf_cache)    -> previous run ka cache
3. edge Space     (TDUBBER_EDGE_URL)            -> 24/7 LAN artifact server
4. huggingface_hub.snapshot_download            -> internet (gated ke liye HF_TOKEN)
```

## Use kaise karein

```python
import sys; sys.path.insert(0, "huggingface")
import hf_store

hf_store.setup_env()                    # Kaggle-aware HF_HOME etc.
path = hf_store.ensure_model("IndexTeam/Index-Homura-2B")
print(path)                             # local snapshot directory
```

## GGUF (torch-free architecture)

PyTorch/CUDA ja raha hai -- `test4_gotgVERSION` ne 1258s me 1067s pip install
me kharch kiye aur phir `libcudart.so.13` par mar gaya. Ab LLM `llama-server`
(llama.cpp) aur ASR `whisper-cli` (whisper.cpp) chalate hain, aur dono ko ek
`.gguf` / `.bin` file chahiye:

```python
import sys; sys.path.insert(0, "huggingface")
import gguf_store

gguf_store.ensure_gguf("homura-2b-q4_k_m")       # 1.31 GB, llama.cpp
gguf_store.ensure_gguf("whisper-large-v3-turbo") # 1.62 GB, whisper.cpp
gguf_store.ensure_gguf(task="asr")               # default for the task
gguf_store.warm_status()                         # kahan kya hai already
```

**Wahi precedence chain** -- mounted → HF_HOME → edge (`/gguf/<file>`) → hub.
Order alag nahi rakha gaya, kyunki ek alag order wahi "silently dead fast path"
hai jo pehle 525s chup-chaap barbaad kar rahi thi.

**Digest kabhi invent nahi hota.** Har roster row par `sha256_verified` hai:

| state | matlab |
|---|---|
| `sha256` + `sha256_verified: true` | publisher/registry se confirm -- verify ENFORCE hota hai |
| `sha256: ""` + `sha256_verified: false` | koi digest nahi -- sirf size check, aur log me loudly likha jaata hai |

Galat digest missing digest se **zorat se bura** hai: wo har future download ke
verification ko hamesha fail karta hai aur ek aisi value naam karta hai jo koi
recognise nahi karta -- koi nahi samajh paata ki published file "corrupt" kyun
rehti hai.

Mismatch par file **delete** ho jaati hai aur `GgufError` raise hota hai.
Chhod dete to next run size-check se use accept kar leta, phir dobara download,
phir dobara reject -- har run me, aur beech me koi bhi reader ko pata nahi
chalta ki wo path safe hai ya nahi.

## HF_TOKEN

* CohereX (Cohere Transcribe + wav2vec2) models **gated** hain --
  `HF_TOKEN` set karo (https://huggingface.co/settings/tokens).
* Homura-2B, Qwen3-TTS, OmniVoice, faster-whisper public hain.
* Token kabhi code me hard-code nahi -- env var se aata hai
  (`.env`, Kaggle notebook Settings → Secrets, ya shell export).

## Kaggle Secrets

Kaggle notebook me: **Settings → Secrets → `HF_TOKEN`** add karo.
Worker cell use `os.environ["HF_TOKEN"]` se padhta hai.

## Cache dataset banane ka rasta (warm runs)

```bash
python huggingface/seed_cache.py --out ./hf_seed
# phir us folder ko Kaggle dataset banao:
kaggle datasets create -p ./hf_seed --dir-mode tar
# aur kaggle_worker.ipynb me attach karo (input dataset ke saath)
```

Agla run `hf_store` ko mounted snapshot dhoondh lega -- **0 download**.
