# HOW TO CLONE — T_Dubber (submodule samet)

Is repo mein `deepclonewebsite` (Deep Site Clone tool) **git submodule** ke roop mein linked hai. Isliye normally `git clone` karne par wo folder **khaali** aa sakta hai. Neeche exact steps hain.

---

## 1. Naya Clone (Kisi Bhi PC Par)

```bash
git clone --recurse-submodules https://github.com/engrtarun/T_Dubber.git
cd T_Dubber
```

`--recurse-submodules` flag se `deepclonewebsite/` folder ki poora content apne aap aa jayega.

> **Note:** Public repo hai, isliye koi login/token nahi chahiye.

---

## 2. Agar Bina Flag Ke Clone Ho Gaya (Folder Khaali Hai)

```bash
cd T_Dubber
git submodule update --init --recursive
```

---

## 3. Submodule Ko Latest Par Lana (Kabhi Bhi)

```bash
cd T_Dubber
git submodule update --remote deepclonewebsite
git add deepclonewebsite
git commit -m "chore: bump deepclonewebsite submodule"
git push
```

---

## 4. Ek Baar Ka Tool Setup (Naye Clone Ke Baad)

Prerequisites:
- **Node.js 20+** — hai: Node v24 ✓
- **Chrome ya Edge** — system browser scraping ke liye use hota hai (alag Chromium download nahi hota)
- **Groq API key (free)** — [console.groq.com/keys](https://console.groq.com/keys) se bana lo (credit card nahi chahiye)

```bash
cd deepclonewebsite/open-lovable
npm install
cp .env.example .env.local
```

`.env.local` khol ke bhar:

```env
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_API_KEY=gsk_your_key_here
```

> `.env.local` gitignored hai, key repo mein kabhi nahi jayegi.

### ⚠️ Zaroori: Model List Patch (Fresh Clone Par)

Tool ke UI mein sirf 3 hardcoded models hain (`codex-5.5`, `gemini-3.5-flash`, `kiro-opus-4.8`) jo **mdbox gateway** ke hain — Groq par ye nahi chalenge. Isliye `app/site-clone/page.tsx` (line 28-32) mein `MODELS` array ko Groq ke models se replace kar:

```ts
const MODELS: Array<{ id: string; title: string }> = [
  { id: 'openai/gpt-oss-120b', title: 'Gpt-OSS-120B' },
  { id: 'openai/gpt-oss-20b', title: 'Gpt-OSS-20B' },
  { id: 'qwen/qwen3.8-27b', title: 'Qwen3.8-27B' },
];
```

Aur `lib/crawl/settings.ts` (line 44) mein default model badal:
`model: s.model || 'openai/gpt-oss-120b',`

> **Note:** Ye patch local hai (submodule ka working tree modified hai). Is machine par sab set hai — is machine par kuch nahi karna. Naye machine par ye 5-minute ka step repeat karna padega. Submodule pointer abhi bhi upstream commit `ab180fb` par hai, isliye clone kisi bhi machine par kaam karega.

---

## 5. Tool Start Kar

```bash
cd deepclonewebsite/open-lovable
npm run dev
```

Phir browser mein khol:

```
http://localhost:3000/site-clone
```

Settings panel (top-right) mein AI model choose kar — Groq ke liye `openai/gpt-oss-120b` (quality) ya `openai/gpt-oss-20b` (fast).

---

## 6. OneDrive Clone Kaise Karana Hai

1. `/site-clone` page par jaa.
2. **Start URL:** `https://onedrive.live.com`, **Depth:** `Structure`, **Outputs:** offline site + design system.
3. **"Create task & open browser to log in"** dabba — real browser window khulegi, usme OneDrive mein login kar.
4. Wapas tool UI par aa ke **"Start"** dabba. Baaki sab automatic (crawl → plan → build → distill → analyze).
5. Output milega `deepclonewebsite/open-lovable/public/site-clone/<domain>/<taskId>/` folder mein (gitignored, repo mein nahi jayega).

---

## ⚠️ Dhyan Dene Wali Baatein

- Sirf wahi sites clone kar jiski permission hai — OneDrive Microsoft ka hai, sirf personal reference ke liye, publicly distribute mat karna.
- Same-domain only crawl — bahar nahi jayega.
- Node modules / clone outputs sab gitignored hain, repo clean rahega.
