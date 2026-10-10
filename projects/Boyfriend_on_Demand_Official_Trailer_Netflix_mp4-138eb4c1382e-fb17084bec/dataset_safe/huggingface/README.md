# 🤗 huggingface/ -- T_Dubber's Hugging Face connector

Ye folder T_Dubber ka **single point of contact** hai Hugging Face ke
saath. Isme sab milega jo jodna aur use karne ke liye chahiye:

| File | Kaam |
|---|---|
| **`SETUP_HF.md`** | **Kaha kya karna hai -- setup steps, token, verify, troubleshoot. Yahan se shuru karo.** |
| `models.json` | **Model roster** -- kaunsa model, kaunsi role (LLM/ASR/TTS), kitne GB, gated ya nahi, Kaggle par kahan pin hoga. |
| `hf_store.py` | **Connector module** -- `ensure_model()` / `setup_env()` / `warm_status()`. Mounted cache → HF_HOME cache → edge Space → hub download, isi order me. |
| `seed_cache.py` | **One-shot pre-pull** -- poore roster ko ek folder me download karke use Kaggle "cache dataset" banane ke liye (warm run = 0 download). |
| `test_hf_store.py` | **Regression tests** -- edge route, Python-version-safe unpack, dataset discovery, aur report/resolver agreement. `python huggingface\test_hf_store.py` |
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
