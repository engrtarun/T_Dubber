# T_Dubber

Clean-Room Reverse Engineering ke liye `deepclonewebsite` (Deep Site Clone tool) ka use kiya gaya hai.

## 📌 Kya Hai Yeh Repo

OneDrive ke UI ka **clean-room reverse engineering** — tool se OneDrive ka offline clone (structure/templates) + design system docs nikale gaye hain, taaki apna version banaya ja sake.

## 📂 Repo Kya-Kya Rakhhti Hai

| Path | Kya hai |
|---|---|
| `deepclonewebsite/` | **Git submodule** — Deep Site Clone tool (Next.js app `open-lovable/` ke andar) |
| `CLONE_PLAN.MD` | Poora plan + current status (kya ho chuka, kya fix hua) |
| `HOW_TO_CLONE.MD` | Clone & setup instructions (submode samet) |
| `Home - OneDrive.html`, `Home - OneDrive by FIREFOX.htm` | OneDrive ke saved reference pages |
| `Refrance Screenshot/` | OneDrive reference screenshots |
| `screenshot-*.png/jpg` | Setup ke dauraan ki screenshots (Groq key, UI, etc.) |

## 🚀 Clone Output Kahan Hai

```
deepclonewebsite/open-lovable/public/site-clone/onedrive.live.com/<taskId>/
```

Isme 6 offline pages (Home, My Files, Shared, Recycle Bin, People, Storage Settings), 21 assets, aur `analysis/design-system.md` (OneDrive ke real design tokens) hain. `index.html` browser mein khol ke dekh sakte ho — offline chalegi.

## 🛠 Setup / Dobara Chalane Ke Liye

Poori detail: **[HOW_TO_CLONE.MD](HOW_TO_CLONE.MD)**

Quick:
```bash
git clone --recurse-submodules https://github.com/engrtarun/T_Dubber.git
cd T_Dubber/deepclonewebsite/open-lovable
npm install
# .env.local mein Groq key (console.groq.com se free) + model patch — HOW_TO_CLONE.MD dekho
npm run dev
# http://localhost:3000/site-clone
```

## ⚠️ Disclaimer

Sirf personal learning/reference ke liye. OneDrive Microsoft ka trademark hai — publicly distribute mat karna.
