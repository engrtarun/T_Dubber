# Supabase setup — drive app ke liye

**Project:** `T DUBBER TEST 4 NETLIFY` · **Region:** South Asia (Mumbai) · **Plan:** Free (NANO) · **Status:** Healthy
**Why Supabase:** Postgres jo Netlify Functions aur Cloudflare Pages dono pe chalta hai, bina raw TCP socket ke.

---

## Steps (is order me karo)

### 1. Credentials copy karo ← **yehi maine chahiye**

Supabase dashboard → apna project → **⚙ Project Settings (gear) → API**

| Kahan | Field | Mera kaam |
|---|---|---|
| Project URL | `Project URL` | `NEXT_PUBLIC_SUPABASE_URL` |
| API Keys → `service_role` | `service_role` (secret) | `SUPABASE_SERVICE_ROLE_KEY` |

> ⚠️ `service_role` key row-level security bypass karta hai — **sirf server pe**. Kabhi `NEXT_PUBLIC_` mat banana, warna browser me chali jayegi.
> Dono safe hain **sirf tab** jab repo me commit na ho (`.gitignore` me `.env*` pehle se hai).

### 2. Schema apply karo

Supabase dashboard → **SQL Editor → New query** → `db/schema.sql` ka poora content paste karo → **Run**

Tables: `users`, `folders`, `files`. Sab `IF NOT EXISTS`, dobara chalane se safe.

### 3. Verify

```powershell
cd drive
Copy-Item .env.example .env.local      # credentials paste karo
npm run db:check
```

Teen `ok` lines aani chahiye. Nahi aaye to bash **Step 2** repeat karo.

---

## Connection string (sirf tab chahiye jab raw SQL chahiye)

Project Settings → **Database → Connection string → URI**

```
postgresql://postgres.PROJECT-REF:PASSWORD@aws-0-ap-south-1.pooler.supabase.com:6543/postgres
```

**Do traps yahan hain:**

1. **Password me percent-encoding chahiye.** Is project me `@` hai password me
   (`@tarundilwal`), aur `@` URI me separator hai — bina encode kiye pura URL
   toot jaata hai. `@` → `%40`.
2. **`:6543` (pooler) direct `:5432` se behtar hai** serverless pe — serverless
   har request pe naya connection kholta hai, aur direct connection free tier ke
   limited slots khaa jaata hai.

---

## Deploy pe ye set karna mat bhoolna

| Platform | Kahan |
|---|---|
| Netlify | Site configuration → Environment variables → dono keys |
| Cloudflare | Pages → Settings → Environment variables → dono keys |

Proof: `GET /api/db-check`

---

## Screenshots

Project banate waqt ke screenshots is folder me rakhne ke liye:

```
drive/docs/supabase-setup/screenshots/
```

Suggested names: `01-create-project.png`, `02-api-settings.png`,
`03-sql-editor-schema.png`, `04-netlify-env-vars.png`.

> Note: chat me paste kiye gaye screenshots disk pe save nahi hue (browser
> session alag hai), isliye ye folder abhi khali hai — files yahan drop karo.