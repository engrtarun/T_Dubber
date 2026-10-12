# T_Dubber — Master Context README

> **Ye file repo ka single source of truth hai.** Koi bhi naya agent/developer
> isko padh ke context le sakta hai — branches, structure, UI plan, deploy,
> aur merge status yahin hai.

---

## 1. Ye repo hai kya

**T_Dubber** = Advanced Video Dubbing Infrastructure — Zero-cost cloud GPU
(Kaggle workers) + Index-Homura-9B model se syllable-controlled dubbing.
Saath me **UI work** chal raha hai (OneDrive-style drive interface ka clone
capture karke apna interface banane ke liye).

Do cheezein is repo me ek saath hain:

| Kaam | Kahan |
|---|---|
| Asli dubbing app (pipeline, workers, Gradio UI) | `main` branch |
| UI/design work — OneDrive clone reference + naya `drive/` web app | `ui` branch |

---

## 2. ⚠️ Branch map (pehle ye samajh lo — sabse important)

| Branch | Kya hai | HEAD | Commits |
|---|---|---|---|
| `main` | Poora T_Dubber app: Python pipeline, Go, Rust/C++ binaries, Gradio UI (`app.py`), Kaggle worker, Telegram upload, 10 submodules | `c8027ec` (v0.1 Zero-Torch) | 70 |
| `ui` | OneDrive clone reference + `deepclonewebsite` submodule + `drive/` (Next.js UI app) + docs | `0b5c8bd` | 5 (ROOT branch) |

### 🚨 Critical: dono branches UNRELATED hain

```
git merge-base main ui   →   (khali — koi common ancestor hi nahi)
```

- `ui` ek **root/orphan branch** hai (nayi history se shuru hui), `main` ki
  history usme kabhi merge nahi hui.
- Matlab: normal `git merge main` **kaam nahi karega** — chahiye
  `--allow-unrelated-histories` + manual conflict resolution (runbook §5).
- Dono side ek hi naam ke files rakhte hain (`README`, `.gitmodules`,
  `.gitignore`) → merge par wahi 3 conflicts aayenge. Exact resolution §5 me hai.

---

## 3. Repo structure (ui branch — current)

```
T_Dubber/
├── README.md                 ← ye file (sabka context)
├── .gitignore                ← node_modules/.next/OS junk (+ merge note)
├── .gitmodules               ← deepclonewebsite submodule
├── docs/
│   ├── CLONE_PLAN.MD         ← OneDrive clone ka poora plan + status
│   ├── HOW_TO_CLONE.MD       ← clone & setup instructions (submodule samet)
│   └── reference/            ← OneDrive ke saved reference pages
│       ├── Home - OneDrive.html (+ _files/)
│       ├── Home - OneDrive by FIREFOX.htm (+ _files/)
│       └── screenshots/      ← setup + reference screenshots
├── drive/                    ← 🛠️ Next.js app — OneDrive UI clone (agent ka kaam)
│   ├── app/                  (layout, redirect page)
│   ├── public/drive-ui/      (35-file capture — BILKUL UNTOUCHED)
│   ├── package.json, tailwind, tsconfig...
│   └── README.md             (drive ke andar ka detail docs)
└── deepclonewebsite/         ← git submodule — Deep Site Clone tool
```

**Folder rules:**

- `docs/reference/` = sirf reference material (HTML saves, screenshots). Kabhi
  app code yahan mat daalo.
- `drive/public/drive-ui/` = capture, **kabhi edit mat karo** (details drive/README.md).
- Root pe kuch mat chhodo — sab `docs/` ya `drive/` ke andar.
- Merge ke baad `main` ka structure (app.py, projects/, references/... etc.)
  bhi root me aa jayega — wo sab main ka hai, alag mat karo.

---

## 4. `drive/` — UI interface (status + chalane ka)

OneDrive ka **clean-room capture** (deepclonewebsite tool se) ek Next.js app
ke through serve hota hai — byte-for-byte, koi edit nahi.

### Chalane ka

```bash
cd drive
npm install
npm run dev        # → http://localhost:3000
```

| URL | Kya hai |
|---|---|
| `/` | redirect → `/drive-ui/index.html` |
| `/drive-ui/index.html` | Home |
| `/drive-ui/pages/my.html` | My Files |
| `/drive-ui/pages/shared.html` | Shared |
| `/drive-ui/pages/recycle.html` | Recycle Bin |
| `/drive-ui/pages/people.html` | People |
| `/drive-ui/pages/page.html` | Storage settings |

### ✅ Verification status (12 Oct 2026, 22:13 — agent ka setup COMPLETE)

- Capture ke **35/35 files** deepclonewebsite source se **SHA256 identical**
  (`deepclonewebsite/open-lovable/public/site-clone/onedrive.live.com/mv3udyvo-npb0/`).
- Dev server par **byte-for-byte serve** verified (served hash == disk hash,
  310280 bytes index.html), redirect 307 + pages + assets sab 200.
- **`npm run typecheck` = PASS (exit 0), `npm run build` = PASS (exit 0)**
  — routes: `/` (static) + `/api/db-check` (dynamic).
- Agent ne **DB layer** bhi banaya: **Supabase Postgres** (pehle Neon tha,
  ab `@supabase/supabase-js`) — `db/schema.sql` = users/folders/files,
  `db/init.mjs` + `db/check.mjs` (`npm run db:check`), `lib/db.ts`,
  `.env.local` (gitignored ✓), `GET /api/db-check` = deploy ka connection
  proof. Setup record: `drive/docs/supabase-setup/` + `drive/docs/setup-screenshots/`.
  Reasoning drive/README.md me (serverless pe SQLite nahi chalta — sahi point).
- Stack: Next.js 15.4.3 · React 19.1 · Tailwind 3.4.17 · TS 5 · Node 24.
- ⚠️ `next@15.4.3` me CVE-2025-66478 hai — deploy/public se pehle upgrade karna
  (drive/README.md me note hai).
- ⚠️ `public/drive-ui/` me `task.json`, `capture-plan.json`, `sitemap.json`,
  `analysis/` bhi publicly serve hote hain — test deploy me chalega, production
  se pehle hata dena.
- 📌 drive/ abhi **untracked** hai (`?? drive/`) — merge se pehle commit karna
  zaroori hai (§5 me steps).

### Deploy — Netlify TEST (org final Cloudflare Pages par hoga)

**Option A — sabse fast (sirf static files, koi build nahi):**

```bash
# drive/ abhi commit nahi hai, isliye CLI drag se kaam chalega
npm i -g netlify-cli
netlify login
netlify deploy --dir=drive/public          # preview URL milega
netlify deploy --dir=drive/public --prod   # test ke liye
# open: https://<site>.netlify.app/drive-ui/index.html
# (/ par 404 aayega — wo Next ka redirect hai, static me nahi; test ke liye OK)
```

**Option B — poora Next.js app (recommended, redirect bhi chalega):**

1. `drive/` ko commit + push karo (main repo ke andar, `ui` branch par).
2. Netlify → "Add new site" → "Import an existing project" → GitHub repo chuno.
3. Build settings:
   - **Base directory:** `drive`
   - **Build command:** `npm run build`
   - **Publish directory:** `.next` (Netlify ka Next.js plugin auto-detect karta hai)
4. ya CLI (drive/ ke andar se): `npx netlify-cli deploy --build`, pehle
   `drive/netlify.toml` banao (exact file drive/README.md §Netlify me hai):

```toml
# drive/netlify.toml  (file drive/ ke andar hai, isliye base ki zaroorat nahi)
[build]
  command = "npm run build"
  publish = ".next"

[build.environment]
  NODE_VERSION = "24"

[[plugins]]
  package = "@netlify/plugin-nextjs"    # API routes (/api/db-check) ke liye zaroori
```

> ❌ `netlify deploy --dir .` mat chalao — wo source+node_modules static
> publish karega aur API routes banenge hi nahi. Static-only test chahiye to
> `--dir=public` (Option A).

**Cloudflare Pages (final/org):** base=`drive`, build=`npm run build`.
Static output chahiye to `drive/next.config.ts` me `output: "export"` add karo
→ build `out/` dega, wo directly Pages/Netlify par bina runtime ke chalega.

---

## 5. 🔧 main ↔ ui merge runbook (jab jodna ho)

Merge se pehle `ui` par ye sab commit ho chuka hai (structure + README +
.gitignore + drive/). Fir:

```bash
git checkout ui
git add drive/ && git commit -m "feat: add drive Next.js UI app"   # agar pending ho
git merge main --allow-unrelated-histories
```

### Expected conflicts (exactly 3) aur unka resolution

| # | File | Kyun | Resolution |
|---|---|---|---|
| 1 | `README.md` | Dono side ne alag content likha (case conflict `README.MD`/`README.md` **fix ho chuka hai** — ab exact same path hai) | Is naye context README ko rakhna hai (superset). Chaaho to main ka landing-page wala README `docs/LANDING.md` me save kar lena |
| 2 | `.gitmodules` | main = 10 submodules, ui = deepclonewebsite | **Union** — saare 11 entries rakhna (copy-paste, koi delete mat karna) |
| 3 | `.gitignore` | Dono side ka alag hai | **Union** — main ka poora content + is branch ka node_modules/.next block |

Merge ke baad:

```bash
git submodule update --init --recursive     # 11 submodules (10 main + 1 deepclonewebsite)
cd drive && npm install && npm run dev      # UI waisa hi chalega
```

**Merge easily safe kyun hoga:**

- `docs/`, `drive/` main par exist hi nahi karte → koi conflict nahi.
- `references/` (main) vs `docs/reference/` (ui) → alag-alag paths, koi case
  clash nahi (isi liye `reference/` root pe nahi rakha gaya).
- `drive/` commit ho chuka hoga → merge usse chhedega nahi.
- OneDrive saved pages/screenshots sirf ui par hain → clean add honge.

---

## 6. Kaunse pages/interfaces chahiye (deep reasoning)

### A. Abhi main branch par kya hai (source of truth: `app.py` + `ui_static/`)

Gradio app `Tarun Dubber AI` — 4 tabs:

1. **🎬 Studio Workspace** — job submit, `### ☁️ Telegram backup`,
   `### 📊 Quality Dashboard` (WER/sync scores — Phase 2)
2. **☁️ Telegram Drive** — Upload Any Size, Restore an Archive, Archive from a
   Link, "What is archived" status, config
3. **📂 Project History** — past projects
4. **🗄️ Database** — SQLite introspection

Plus: sticky **topbar** (brand, live clock, job timer, settings drawer),
**Puter.js AI assistant** (log explain karta hai — Funny/Serious/Roast
personas), aur alag `dashboard/index.html` + `dashboard_server.py`.

### B. 2026 streaming-UI benchmark (web search findings)

- **Best overall interface = Netflix** (MakeUseOf 2026 ranking:
  Netflix > Max > Peacock > Hulu > Disney+ > Apple TV+ > Prime Video > Paramount+).
  Tom's Guide 2026: koi bhi perfect nahi — highest grade Apple TV+ = B.
  Industry consensus (Variety): Netflix = "gold standard".
- **4–5 destinations max** (Home / Live / Library / Profile) — Fora Soft 2026
  7-pillar model. Zyada tabs = IA overload.
- **"Continue Watching" = sabse high-value element** — top of Home, hamesha accurate.
- **Design language:** cinema-black (`#000000`–`#2d2d2d`), EK accent color
  (Netflix red `#e50914` sirf primary CTA par), 8px radius, weight-900 hero
  + weight-400 body, secondary text `#b3b3b3`.
- **Performance budgets 2026:** INP < 200ms, time-to-first-frame < 2s,
  rebuffer < 1%, **3 taps me content tak pahunchna**, WCAG 2.1 AA.
- Netflix naya mobile UX: vertical feed se discovery (swipe = thumb becomes
  the interface) — decision-friction kam karta hai.

### C. T_Dubber ke liye recommended page list (reasoning samet)

| # | Page | Kyun chahiye (reasoning) |
|---|---|---|
| 1 | **Home / Studio** | Default landing. Top par "Active & Recent Jobs" rail — *Continue Watching ka analog*: user jahan ruka tha wahi se aage badhe. Neeche "New Dub Job" primary CTA (ek hi accent color yahan). |
| 2 | **Job Detail (pipeline view)** | Streaming ka *player ergonomics* = yahan **stage-by-stage progress** (Ingest → Kaggle GPU → TTS → Assemble → Telegram backup), live logs, ETA, retry. Sabse critical page — yahin user mins guzarta hai, isliye INP/log-tail speed matter karti hai. |
| 3 | **Library / Projects** (Project History) | *Library tab* analog — past dubs, search (typo-tolerant), filters (status/language/date), thumbnail grid + "continue" button. |
| 4 | **Telegram Drive** | Storage surface — upload/restore/archive-from-link + "what is archived" status. Ye alag tab rakho (main me bhi hai), kyunki iska user mental-model storage ka hai, dubbing ka nahi. |
| 5 | **Quality Dashboard** | Phase 2 ka differentiator — WER, sync offsets, speaker similarity per job. Job detail ke andar section + upar aggregate (Netflix ki "Top 10 row" tarah ek high-signal rail). |
| 6 | **Topbar + Settings drawer + AI Assistant** | Already exist (`ui_static/`). Clock/job-timer/assistant ko wahi rakho — global chrome hai, page nahi. |
| 7 | **Database (admin)** | Debug/low-level — last me, nav me hidden rakho (admin-only, "Profile" wali corner jagah). |
| 8 | ~~Auth/Profiles~~ | **Skip** — single-user local tool. Multi-user hua to sochna. |

**IA rule:** max 4 bottom/side destinations → `Home`, `Library`, `Drive`,
`Settings`. Baaki sab Job Detail ke andar ya drawer me.

**Visual rule (Netflix se):** dark canvas, EK accent (T_Dubber blue
`#0078D4` — capture ke design-system.md se), content/thumbnails hi chroma
laayein, chrome minimal.

---

## 6.5 Data architecture — SQLite vs Postgres vs Telegram (kya kahan)

Teen "databases" hain aur **teeno ka role alag hai — judne ki zaroorat nahi,
bridge = application layer:**

| Store | Kaun use karta hai | Kya rakhta hai | Kyun wahan |
|---|---|---|---|
| **SQLite** `t_dubber.db` (main, `db.py`) | Local dubbing app | `projects`, `job_queue`, `pipeline_stages`, `quality_metrics`, `speakers` + **`telegram_archives`/`telegram_parts`/`channel_daily_quota`** (TG ka ledger!) | Single-writer local telemetry — waisa hi sahi |
| **Telegram** (tgup/Telethon) | Dono apps | **File BYTES** — videos, dubbed output, archives (chunks, channels) | Ye hamara **asli free cloud/object storage** hai — unlimited-ish, 1900 MiB parts, direct-link upload |
| **Postgres** (Supabase) | `drive/` app (serverless) | Sirf **metadata**: `users`, `folders`, `files(storage_key, size, mime)` | Serverless pe filesystem ephemeral; Supabase free tier ke bytes budget me nahi — isliye bytes yahan NAHI, sirf index |

**Data flow (jab ek hi cheez teeno me chahiye):**

```
dub output (main app)
  → tgup se TG channel me upload        [BYTES → Telegram]
  → SQLite me ledger: archive/parts/msg  [INDEX → SQLite]
  → drive ke files.storage_key = TG ref  [CLOUD INDEX → Postgres]
  → drive UI (Netlify/CF) list dikhaye, download TG se
```

**Rules:**
1. SQLite ↔ Postgres ka **direct sync nahi** — alag engines, alag machines.
   Chahiye to application layer se one-way push/pull (drive ka API ya export
   script). Aaj zaroorat nahi — dono apps alag hain.
2. **Bytes kabhi Postgres me mat daalo** (Supabase free = ~500MB) — bytes
   Telegram pe, Postgres me sirf `storage_key` (TG message ref/link).
3. `files.storage_key` ka intended meaning yahi hai: object-storage pointer —
   wo pointer **Telegram** ban sakta hai (Supabase Storage se bhi chalega,
   par TG = zero cost + unlimited).

---

## 7. Setup / clone

Detail: **[docs/HOW_TO_CLONE.MD](docs/HOW_TO_CLONE.MD)** ·
Plan/status: **[docs/CLONE_PLAN.MD](docs/CLONE_PLAN.MD)**

```bash
git clone --recurse-submodules https://github.com/engrtarun/T_Dubber.git
git checkout ui            # UI work ke liye
cd T_Dubber/drive && npm install && npm run dev    # → localhost:3000
# Gradio app (main branch): cd deepclonewebsite/open-lovable && npm install
# .env.local me Groq key (console.groq.com) — docs/HOW_TO_CLONE.MD dekho
```

---

## 8. Conventions (naye agents ke liye)

1. **Ye README update karte raho** — naya kaam/kabhiya? isme section add karo
   taaki agla agent context kho na de.
2. `drive/public/drive-ui/` = capture, **read-only**. UI change chahiye to
   `app/` ya naye component me karo.
3. Root pe koi loose file nahi — sab `docs/`, `drive/` ke andar.
4. `main` me directly commit mat karo jab tak merge plan na chal raha ho —
   UI ka saara kaam `ui` branch par.
5. Screenshots: `docs/reference/screenshots/`.
6. Deploy: test Netlify (§4), final **Cloudflare Pages** (org standard).

---

## ⚠️ Disclaimer

Sirf personal learning/reference. OneDrive/Microsoft, Netflix wagairah ke
trademarks unke hain — publicly distribute mat karna.
