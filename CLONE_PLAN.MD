# CLONE PLAN — OneDrive UI Clone (STATUS: ✅ COMPLETE)

> Yeh file originally ek plan thi. Ab isme **current status** hai — kya-kya hua, kya-kya fix hua, aur output kahan hai.

---

## 🎯 Goal Kaunsa Tha

OneDrive ka UI clone karna — ek **offline reference site** jisme tera apna logged-in OneDrive ka structure/templates capture ho (files page, people, recycle bin, shared, etc.), saath mein **design system tokens** ka AI-generated doc.

## ✅ Kya-Kya Ho Chuka

| Step | Status | Detail |
|---|---|---|
| Tool repo submodule | ✅ | `deepclonewebsite/` (hi5jeff/deepclonewebsite, commit `ab180fb`) |
| Dependencies | ✅ | `open-lovable/` mein `npm install` (Node v24 par) |
| AI Gateway | ✅ | **Groq** (free tier) — `https://api.groq.com/openai/v1` |
| API Key | ✅ | Groq console se, `.env.local` + `.clone-settings.json` mein set (gitignored) |
| Model patch | ✅ | UI ke hardcoded mdbox models → Groq models (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) |
| Login | ✅ | OneDrive login browser profile mein save (`.crawl-sessions/profiles/`) — dobara login nahi karna |
| Crawl | ✅ | 12 pages / 11 types (onedrive.live.com) |
| Plan + Build | ✅ | Bug fixes ke baad (neeche dekho) |
| **Offline site** | ✅ | **6 pages** capture hue (neeche manifest) |
| **Design system** | ✅ | `analysis/design-system.md` — real tokens (#0078D4 Microsoft blue, #005A9E hover, #00A4EF accent...) |

## 📦 Output Kahan Hai

```
deepclonewebsite/open-lovable/public/site-clone/onedrive.live.com/mv3udyvo-npb0/
├─ index.html              ← Home (open this)
├─ pages/my.html           ← My Files
├─ pages/shared.html       ← Shared Files
├─ pages/recycle.html      ← Recycle Bin
├─ pages/people.html       ← People
├─ pages/page.html         ← Storage Settings
├─ assets/                 ← 21 SVG/ICO/WebP assets
├─ analysis/design-system.md  ← design tokens + component specs
├─ manifest.json           ← page list + asset counts
├─ sitemap.json            ← crawled structure
├─ capture-plan.json       ← LLM ka capture strategy
└─ task.json               ← full task log
```

Offline site dekhne ke liye: `index.html` par double-click karke browser mein khol (sab relative paths hain, offline chalegi).

## 🐛 Bugs Jo Fix Hue (Fresh Clone Par Dhyan Do)

1. **LLM 404 model_not_found** — tool model name se `openai/` prefix kaat raha tha (`lib/crawl/llm.ts`). Groq par asli ID `openai/gpt-oss-120b` hai. Fix: strip hataya.
2. **JSON parse crash (`Expected property name...position 1`)** — gpt-oss (reasoning model) prefill `{` ko echo karke poora JSON bhejta hai, tool upar se ek aur `{` jod raha tha → `{{...`. Fix: `plan-strategy.ts` mein brace sirf tab prepend hota hai jab model ne na diya ho.
3. **Empty LLM output** — `maxTokens: 4000` reasoning tokens mein khatam ho jate the. Fix: 16000 kiya (plan + analyze dono).
4. **Crawl marketing pages par chala gaya** — bina login ke `onedrive.live.com/` → microsoft.com marketing site redirect hota hai. Fix: login ke baad hi task run karna (profile save hone ke baad automatic).

> Ye sab fixes `deepclonewebsite` submodule ke **local working tree** mein hain. Parent repo pointer upstream `ab180fb` par hai — clone kahin bhi kaam karega. Details `HOW_TO_CLONE.MD` mein.

## 📝 Screenshots (Renamed)

| File | Kya hai |
|---|---|
| `screenshot-groq-api-key-console.jpg` | Groq console ka API key wala screenshot |
| `screenshot-openai-free-api-search.png` | "openai api key free" search result (OpenAI free nahi deta isliye Groq chuna) |
| `screenshot-site-clone-ui-initial.png` | Tool UI ka pehla screenshot (patch se pehle) |
| `Refrance Screenshot/screenshot-onedrive-reference-page.png` | OneDrive reference page |

## ⚠️ Notes

- Sirf personal reference ke liye — OneDrive Microsoft ka hai, publicly distribute mat karna.
- Clone outputs `public/site-clone/` mein hain jo gitignored hain — repo saaf rahegi.
