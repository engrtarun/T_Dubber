# Setup screenshots

Ye project ka **asli setup record** hai — Supabase project aur Netlify token
 banane ka process, apne hi screenshots me.

| File | Kya dikhata hai |
|---|---|
| `01-supabase-create-project.png` | Project creation form — name, DB password, region **South Asia (Mumbai)**, security options |
| `02-supabase-project-dashboard-SECRET-pw-visible.png` | Project dashboard: **Healthy**, NANO, `ap-south-1`, "Get connected" panel |
| `03-netlify-create-access-token.png` | Personal access token form — description, "No expiration" |
| `04-netlify-token-generated-SECRET-revoke-after-use.png` | Generated token + "one time only" warning |
| `05-netlify-applications-settings.png` | Applications settings page (OAuth apps + tokens list) |
| `10-live-netlify-home.png` | 🟢 **Live deploy** — Home page (netlify.app) |
| `11-live-netlify-my-files.png` | 🟢 **Live deploy** — My Files page (netlify.app) |

---

## 🚨 Do screenshots me live secrets hain

Naam me `SECRET` likha hai isliye ki koi galti se commit na kar de:

| File | Secret jo dikhta hai | Kya karna hai |
|---|---|---|
| `02-…SECRET-pw-visible.png` | **Supabase DB password** (`@tarundilwal`) — Firefox ke save-password popup me plain text | **Rotate karo**: Supabase → Settings → Database → Reset database password |
| `04-…SECRET-revoke-after-use.png` | **Netlify personal access token** (`nfp_…`) | **Revoke karo** deploy ke baad: Netlify → Applications → Tokens → Options → Delete |

Ek extra baat: DB password `@tarundilwal` naam se derive hua hai — ye weak
password hai bhi. Rotate karte waqt koi strong random password daal, aur yaad
rakh `@` ko connection string me **percent-encode** karna hota hai (`%40`).

---

## Repo me kya na ho

Ye folder abhi **untracked** hai. Inhe kabhi commit na karo:

```powershell
# agar git me daalna hi ho to pehle ye screenshots hata do
Remove-Item drive\docs\setup-screenshots\*SECRET*.png
```

Credentials kabhi file me mat likho — `.env.local` gitignored hai, wahin rakho.