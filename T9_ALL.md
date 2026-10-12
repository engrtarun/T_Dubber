# 🌐 T9 Ecosystem — Master Brand & Vision

> **Ye file POORE ecosystem ka north-star hai.** Koi bhi AI agent ya developer
> isko padhke samjhega: T9 kya hai, kaunse products hain, aur har product kya
> banata hai. Naya product yahin add hota hai.
>
> **Agent rule:** koi bhi T9 product par kaam shuru karne se pehle ye file padho,
> aur kaam khatam hone par ise update karo (jaisa root `README.md` ka rule hai).

---

## 1. T9 kya hai

**T9** ek **AI-powered content + media ecosystem** hai — ek hi brand ke neeche
kai products, ek hi storage aur ek hi account.

| | |
|---|---|
| **Brand** | T9 |
| **Tagline** | *Create. Store. Stream. Dub.* |
| **Origin** | T_Dubber repo se nikaala gaya (dubbing pipeline + storage) |
| **Parent repo** | `github.com/engrtarun/T_Dubber` |

### T9 ke teen pillars

```
        T9 CLOUD  ──▶  storage + compute  ──┐
                                          ├──▶  T9 DUBB (AI dubbing)
        T9 EDITOR  ──▶  content authoring ─┤
                                          ├──▶  T9 PLAY (streaming)
        T9 PUTTER AI ▶  AI generation  ────┘
                                          └──▶  T9 NEWS (publishing)
```

---

## 2. Products

### ☁️ T9 CLOUD — storage + compute
- **Kya hai:** T9 ka storage aur compute layer. Files, media, AI models.
- **Status:** 🟢 **Live** — https://t9-cloud-381.netlify.app
  (Next.js 15 · Tailwind · TypeScript · Supabase Postgres · Netlify)
- **Repo:** [`drive/`](drive/README.md)
- **Key idea:** storage quota bekaar hai — **Telegram hi asli storage hai**
  (effectively unlimited, ~1900 MiB parts, direct-link resume). Cloud sirf
  **index + metadata + UI** rakhta hai, bytes nahi.
- **Rule:** kabhi bhi kisi T9 product me bytes DB me mat daalo. Bytes →
  Telegram. DB me sirf `storage_key` (pointer).

### 🎬 T9 DUBB — AI dubbing pipeline *(T_Dubber)*
- **Kya hai:** Video dubbing — original video → dubbed video, syllable-level sync.
- **Status:** 🟡 Core pipeline `main` branch par. **Zero-Torch** (koi CUDA/PyTorch nahi).
- **Repo:** `main` branch · `app.py` + `multitasker.py` + `pipeline.py`
- **T9 Cloud se taalluq:** dubbed output → T9 Cloud me store → T9 PLAY me stream.

### ✏️ T9 EDITOR — content authoring
- **Kya hai:** Script, storyboard, captions, subtitle authoring.
- **Status:** ⬜ Planned — design tokens [`drive/public/drive-ui/analysis/design-system.md`](drive/public/drive-ui/analysis/design-system.md) se aayenge.
- **Note:** editor authoring karta hai, T9 DUBB usko bolta/dubb karta hai.

### 🎨 T9 PUTTER AI — AI generation
- **Kwa hai:** Images, thumbnails, voice, music, text — generative AI.
- **Status:** ⬜ Planned. Provider-agnostic (Qwen, OmniVoice, VibeVoice sab torch-free).
- **Note:** TTS ab bhi **Candle/Rust** (`tts_forge`) se chalta hai — koi PyTorch nahi.

### ▶️ T9 PLAY — streaming
- **Kya hai:** Finished media stream karna (Telegram se direct-link).
- **Status:** ⬜ Planned. Telegram = free CDN (no storage cost).

### 📰 T9 NEWS — publishing
- **Kya hai:** Content publish, feed, share.
- **Status:** ⬜ Planned.

---

## 3. Design language (sab products me same)

Source of truth: **`drive/public/drive-ui/analysis/design-system.md`**
(OneDrive ke real tokens se nikale gaye — clean-room reference).

| Token | Value | Use |
|---|---|---|
| Primary | `#0F6CBD` | CTA, active, brand |
| Primary hover | `#115EA3` | hover state |
| Accent | `#00A4EF` | highlight, link |
| Success | `#107C10` | done |
| Warning | `#DA3B01` | storage/full states |
| Dark bg | `#292929` / `#1F1F1F` / `#141414` | dark surfaces (1/2/3) |
| Light bg | `#FFFFFF` / `#FAFAFA` / `#F5F5F5` | light surfaces |

**Rules:**
1. **Dark by default** — T9 Cloud me already apply hai (`data-theme="fluent-dark"`).
2. **Ek accent per screen** — Netflix-style, ek hi colour CTA par.
3. **Content hi chroma laaye** — chrome minimal rakhna.
4. **Telegram bytes, cloud metadata** — har product me yehi rule.

---

## 4. Cross-product rules

1. **Storage = Telegram.** Cloud/database me bytes kabhi nahi. `storage_key` pointer.
2. **Zero CUDA, zero PyTorch** — T9 ka pura stack torch-free hai (Candle/Rust/Go).
   Kaggle run `test4_gotgVERSION` ne 1067s torch+CUDA me barbaad kiya tha.
3. **SQLite local, Postgres serverless.** Desktop/Kaggle → SQLite
   (`t_dubber.db`); Netlify/Cloudflare → Postgres (filesystem ephemeral hai).
4. **Branding ke liye capture edit mat karo.** `drive/public/drive-ui/` =
   pristine reference (byte-identical). T9 branding **derived** hai:
   `npm run brand` → `drive/public/t9-cloud/`.

---

## 5. Live links

| Kya | Link |
|---|---|
| 🟢 T9 Cloud (live) | https://t9-cloud-381.netlify.app |
| T9 Cloud (previous test build) | https://t-dubber-drive-test.netlify.app |
| Repo | https://github.com/engrtarun/T_Dubber/tree/ui |
| Live DB proof | `<site>/api/db-check` |

---

## 6. Roadmap ek nazar me

| # | Product | Stage | Status |
|---|---|---|---|
| 1 | T9 CLOUD | Storage + infra | 🟢 Live |
| 2 | T9 DUBB | Dubbing engine | 🟡 Core ready |
| 3 | T9 EDITOR | Authoring | ⬜ Next |
| 4 | T9 PUTTER AI | Generative | ⬜ Planned |
| 5 | T9 PLAY | Streaming | ⬜ Planned |
| 6 | T9 NEWS | Publishing | ⬜ Planned |

---

## 7. Naye agent ke liye

1. **Ye file padho** pehle — brand vision yahin hai.
2. Naya product banaye ho to **§2 me add karo** (status + repo + ek line).
3. Design tokens **§3** se uthao, invent mat karo.
4. Cross-product rules **§4** kabhi mat toda — khaas kar "bytes Telegram me".
5. Kaam khatam hone par **§6 roadmap** update karo.
6. T9 Cloud branding chahiye to `drive/` me `npm run brand` chalao — capture
   (`drive-ui/`) ko haath mat lagana.

---

<div align="center">
<sub>T9 · part of <a href="README.md">T_Dubber</a> · Updated 12 Oct 2026</sub>
</div>