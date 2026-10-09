# 🤗 Hugging Face setup — kaha kya karna hai

Ye file **kaam karne ke liye** hai. Architecture ke liye `README.md` padho,
project me HF ka role kaha hai wo `../KAGGLE_HF_PLAYBOOK.md` me hai.

TL;DR — 3 steps, ~10 minute:

```powershell
# 1. token banao aur rakho  (HF_TOKEN)
#    https://huggingface.co/settings/tokens  -> Read token
notepad HuggingFace_PaperWork\hf.env          # HF_TOKEN=hf_xxxxxxxx

# 2. check karo ki token chala
python huggingface\hf_store.py

# 3. (optional, ek baar) local weights cache bana lo
python huggingface\seed_cache.py --out hf_seed
```

---

## 0. Pehle samjho: HF is project me kya karta hai

Project me HF sirf **weights ka address book** hai. Har model ka kaam alag hai:

| Repo | Kaam | Kaggle me kahan lagta hai |
|---|---|---|
| `IndexTeam/Index-Homura-2B` | **translation LLM** (~5 GB) | GPU 0 par `vllm serve` — dubbing ka dimag |
| `Systran/faster-whisper-large-v3` | **ASR** (~3 GB) | source video se transcript nikalna |
| `k2-fsa/OmniVoice` | **TTS** (~2 GB) | Hindi voice generate karna |
| `Qwen/Qwen3-TTS-12Hz-1.7B-Base` | voice-clone TTS (~4 GB) | `--tts-engine qwen` |
| `ResembleAI/chatterbox` | backup TTS (~1 GB) | `--tts-engine chatterbox` |
| `bakrianoo/mazinger-dubber-profiles` | voice profiles (dataset) | `mazinger.profiles.fetch_profile()` |

Poori list code me hai: **`huggingface/models.json`**. Us file ko edit karo
agar model badalna ho — baaki sab usi ko padhta hai. Do jagah list duplicate
nahi karni.

**Gated sirf CohereX hai** (optional ASR). Baaki sab public hain — token ke
bina bhi download hote hain. Token ki zaroorat sirf tab padegi jab `--transcribe-method coherex`
chuno.

---

## 1. Token kahan rakhein

**Yahan:** `HuggingFace_PaperWork/` folder (repo me already hai, git-ignored hai).

TEEN tarah se padha jaata hai, is order me:

| File | Format | Kab use karo |
|---|---|---|
| `hf.env` | `HF_TOKEN=hf_xxxx` | **recommended** — ek hi line |
| `.env` | same | dotenv style prefer karo to |
| `token.txt` | raw token, ek line | sabse simple |

Notebook secrets se (`HF_TOKEN` env var) wo **sabse upar** aata hai — Kaggle
ke liye yehi sahi tareeka hai.

```powershell
# banao (agar folder nahi hai)
New-Item -ItemType Directory -Force HuggingFace_PaperWork
notepad HuggingFace_PaperWork\hf.env
```

**Token kabhi commit mat karo.** `.gitignore` already cover karta hai, par
check kar lena ki `git status` me token file nahi aa rahi.

---

## 2. Verify — yehi sabse important step hai

```powershell
python huggingface\hf_store.py
```

Ye **roster print karta hai** aur har model ke liye batata hai ki wo local hai
ya nahi:

```
warm 0/6 (HF_HOME=C:\Users\pocot\.cache\huggingface)
  missing  IndexTeam/Index-Homura-2B                ~5GB [translation-llm]
  missing  k2-fsa/OmniVoice                         ~2GB [tts]
  ...
```

Yehi output Kaggle worker bhi start pe print karta hai. **Matlab agar ye
`missing` dikha raha hai to run slow hogi, aur log bata dega ki kyun.** Isliye
run se pehle dekh lo — andha nahi hona padta.

`note:` line matlab — model ka folder to hai par weights nahi (beech me ruk
gaya download). Wahan run dobara download karega. Ye pehle galat report hota
tha, ab theek hai.

---

## 3. Weight kahan se aati hai (order)

`hf_store.ensure_model()` ye order try karta hai, **sabse sasta pehle**:

```
1. /kaggle/input        mounted dataset   -> 0 second, 0 byte     ← BEST
2. HF_HOME              pichli run ka cache -> local disk
3. edge Space           24/7 apna server   -> LAN speed
4. huggingface_hub      internet          -> 525 second           ← SLOWEST
```

Local PC pe sirf (2) aur (4) kaam karte hain. Kaggle pe (1) se hi time bachta
hai — wo `seed_cache.py` se banata hai (neeche).

### 3a. Fastest: cache dataset (Kaggle ke liye, ek baar)

Ye weights ko ek Kaggle *dataset* bana deta hai. Phir wo notebook ka **input**
mount ho jaata hai → download zero.

```powershell
python huggingface\seed_cache.py --out hf_seed      # ~15 GB, ek baar
```

phir Kaggle CLI se:

```bash
kaggle datasets create -p hf_seed --dir-mode tar
```

aur `kaggle_worker.ipynb` me us dataset ko **input me attach** kar do
(right sidebar → Add Input → Your Datasets).

Ab har run pe `hf_store.py` `mounted` dikhayega aur Homura ka 525 second →
0 second ho jaayega. Roster change hone pe hi dobara banana padta hai.

### 3b. edge Space (optional, advanced)

Repo ka apna 24/7 artifact server (`edge/`, Go). `TDUBBER_EDGE_URL` set karo
to step 3 activate ho jaata hai.

> **Note:** is path pe abhi ek serious bug fix hua tha. Pehle `hf_store`
> `/artefacts/` (plural) maang raha tha jabki Space `/artifact/` (singular)
> serve karta hai — matlab har request 404 hoti thi aur chup-chaap hub pe gir
> jaata tha. Log me kuch nahi dikhta tha, run chalta rehta tha. Test ab ye
> enforce karta hai: `huggingface/test_hf_store.py`.

---

## 4. Kaggle notebook me kya set karna hai

Notebook → **Settings → Secrets**:

| Secret | Zaroori? | Kyun |
|---|---|---|
| `HF_TOKEN` | sirf CohereX ke liye | gated model download |
| `TDUBBER_MULTITASKER=1` | haan | 3-thread pipeline (overlap) |
| `TDUBBER_LIP_SYNC=1` | optional | mouth re-render |
| `TDUBBER_TG_CHANNEL` + `TDUBBER_TGUP_CREDS` | optional | Telegram checkpoint |
| `TDUBBER_EDGE_URL` | optional | edge Space URL |

Local pe `.env.example` ka copy bana lo (`huggingface/.env.example` →
`huggingface/.env`).

---

## 5. Troubleshooting

| Dikhta hai | Matlab | Kya karo |
|---|---|---|
| sab `missing` | cache khaali | normal hai step 1 me — `seed_cache.py` chalao |
| ek model `missing`, baaki `mounted` | wo dataset me nahi tha | `seed_cache.py` dobara |
| `note: dir present, no weight files` | beech me ruk gaya download | `/kaggle/working` saaf kar ke dobara |
| `could not fetch ... 401` | token galat/expired | `HF_TOKEN` dobara check karo |
| `edge miss ...` | edge Space ya route | `TDUBBER_EDGE_URL` hata do — hub pe gir jayega, sirf slow |
| Kaggle pe `ModuleNotFoundError: huggingface_hub` | pip nahi | notebook cell 1 me `pip install -U huggingface_hub` |

**Sabse aam galti:** `mounted` ko expect karna jab dataset attach hi nahi kiya.
Attach kiye bina `mounted` kabhi nahi aayega.

---

## 6. Files

| File | Kaam |
|---|---|
| `models.json` | roster — **ye edit karo** model badalne ke liye |
| `hf_store.py` | resolver: mounted → HF_HOME → edge → hub |
| `seed_cache.py` | ek baar: poore roster ka cache dataset |
| `test_hf_store.py` | regression tests — **fix ka proof** |
| `.env.example` | template, copy karo `.env` banao |
| `SETUP_HF.md` | ye file |

Run karne se pehle:

```powershell
python huggingface\test_hf_store.py    # 16 checks, ~2 second
python huggingface\hf_store.py         # current warm status
```