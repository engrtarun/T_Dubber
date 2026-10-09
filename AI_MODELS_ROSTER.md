# 🤖 T_Dubber: The AI Squad Roster (OpenCode Edition)

Yeh file humari "Manager's Diary" hai. Isme humne record kiya hai ki kaunsa AI (LLM) kis level ka hai, uski kya takat hai, aur usse kaunsa kaam karwana chahiye taaki future mein humein pata rahe ki kis "Yoddha" ko kahan bhejna hai.

---

## 🏆 Tier 1: The Elites (Core Architecture & Heavy Lifting)

### 1. Ling 3.1 Flash Free (The Senior Architect)
- **Role:** Backend Logic, Complex Algorithms, Failsafe Mechanisms.
- **Track Record:** `chop_drop.py` banaya. Kaggle ke timeouts, FFmpeg streaming, aur disk space management ko perfectly handle kiya.
- **Strengths:** 262K context window. Code bilkul clean aur production-ready likhta hai. Error handling mein master.
- **Kab Use Karein:** Jab bhi koi hardcore Python logic, streaming, ya critical system architecture design karna हो.

### 2. Space Bunny Free (The Visionary & UI Master)
- **Role:** Docker Containers, UI/UX Design (Frontend), Multimodal Tasks.
- **Track Record:** 1 Million Token context! (Stealth model).
- **Strengths:** Design aur aesthetic sense bohot tagda hai. Agar poore project ka code ek saath padhna ho (huge context), toh isko use karo.
- **Kab Use Karein:** Jab Premium Dashboard UI banana ho, ya aisi Dockerfile likhni ho jisme 10 alag-alag languages aur tools ek sath pack karne hon.
- **Note (2026-10-08, NOVA):** Isi model ke ek session ne **NOVA** role liya — Telegram ingress/egress (link → channel → Kaggle worker). Upar ka "1M context" claim **verify nahi hua**: doosre models ke specs ka koi reliable data mere paas nahi hai. Jo claim verified nahi hai use factual ke barabar mat likho.
- **Track Record (NOVA session):** P0 land — Tier 0 dedup (**200 MB duplicate upload: 199.8 s → 0.00 s**), content-keyed resume (**65.6 s → 30.6 s**), dataset se media hata. Saare numbers `bench_p0.py` se measured. Blocked: P1 (tgup session unauthorized). Detail: `WE_ARE_TEAM.MD`, `DIRECT_LINK_TG_UPLOAD.md`.

---

## ⚡ Tier 2: The Fast Executors (APIs & Cloud)

### 3. MiMo-V2.6-Flash Free (The Cloud Hacker)
- **Role:** Webhooks, Serverless Scripts, API "Jugaad".
- **Strengths:** Yeh speed mein bohot tez hai aur cloud environments (Cloudflare, AWS) ko ache se samajhta hai. Complex API workarounds nikalne mein sharp hai.
- **Kab Use Karein:** Jab kisi aisi API se kaam nikalwana ho jo officially allowed na ho (jaise Kaggle API ko Cloudflare se trigger karna), aur POST/FormData requests banani hon.

### 4. LongCat 2.5 Preview Free (The Reader)
- **Role:** Documentation Analysis, Codebase Summarization.
- **Strengths:** Naam se hi pata chalta hai "Long Context". Yeh lakho lines ka code ek baar mein padh kar samajh sakta hai.
- **Kab Use Karein:** Jab humara T_Dubber project bohot bada ho jaye aur humein kisi naye developer ke liye poori documentation ya flowchart banwana हो.

---

## 🛠️ Tier 3: The Scripters (Proceed with Caution)

### 5. Nemotron 3.5 Lightning Free (The Confident Junior)
- **Role:** Simple scripts, syntax formatting, notification bots.
- **Track Record:** Telegram Bot webhook ka structure acha banaya, par Kaggle API endpoints hallucinate (fake) kar diye.
- **Strengths:** Syntax bohot clean hota hai, execution fast hai.
- **Weaknesses:** Confidence itna zyada hai ki galat API endpoint ko bhi sach maan kar code likh deta hai. Iska code bina Manager ke review ke pass nahi karna chahiye.
- **Kab Use Karein:** Chhote tasks (jaise Python mein request bhejna, ya JSON parse karna). API architecture isko mat do.

---

## 🎭 Tier 4: The Wildcards (Untested / Specialized)

*In models ko hum future mein chote-mote experiments ke liye use karenge:*

- **Fledge Alpha Free:** Shayad experimental features ya alpha-testing scripts ke liye acha ho.
- **Muse Spark 1.3 Free:** Creative writing. Agar humein T_Dubber ko promote karne ke liye GitHub README ya marketing story likhwani ho, toh Muse Spark sabse best rahega.
- **Ling 3.0 Flash Fin Free:** Financial ya cost-calculation (Jaise Kaggle vs AWS GPU costs compare karna).
- **Big Pickle:** Naam ajeeb hai, shayad kisi specific niche ke liye hai. Isey abhi hold par rakhte hain.
- **Nemotron 3 Ultra Free:** Agar 3.5 Lightning fail hota hai, toh yeh heavy weight version try kar sakte hain local processing ke liye.

---

** MANAGER'S RULE:**
Kisi bhi AI se direct code run mat karwao. Pehle usko Architecture ka flow batao, usse code maango, aur khud review karne ke baad hi Master branch mein merge karo! 
🚀 Moon Mission always requires Mission Control!
