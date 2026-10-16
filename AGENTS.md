# AGENTS.md — T_Dubber: MANDATORY ENGINEERING DIRECTIVE

> **Ye file rule hai, log nahi.** Har AI agent (OpenCode, Claude Code, Cursor,
> Copilot, Cline, Windsurf) jo is repo me code likhe ya badle, uske liye
> **shaktshast (binding)** hai. Chat me koi bhi instruction iske khilaf nahi.
>
> **Code kahan hai?** → [`CODE_MAP.md`](CODE_MAP.md). Usse pehle padh le.
> **Rules kya hain?** → ye file.
>
> `AGENTS_MAP.md` ek **append-only coordination log** hai (kaun kis topic pe
> kaam kar raha hai + post-mortems). Usme rule mat daalo — agle session report
> me dabaa diya jayega. Rules sirf yahan rehte hain.

**Repo:** T_Dubber — Python + Go + Rust + C++ video dubbing/translation
pipeline, Telegram cloud storage, Kaggle GPU workers.

**Applies to:** repo root ka first-party Python. `deepclonewebsite/`,
`mazinger/`, `Index-Translate/`, `references/*` alag projects hain (unke apne
rules hain) — in par ye directive nahi lagti.

---

## 0. GOLDEN RULE — kuch bhi mat toda

```
Koi bhi change jo existing behaviour badal sakta hai, wo AGENT KI
RESPONSIBILITY hai revert karna. "Maine to sirf cleanup kiya" kaam nahi karta.
```

Is repo ka history isiliye hai ki 5 se zyada engineers/agents ne baari-baari
pipeline crash kiya hai. Test pass karna = kaam khatam **nahi** hai.

---

## 1. RUFF — `pyproject.toml` [tool.ruff] se configured hai

Config ke liye koi flag mat likho. Sab kuch `pyproject.toml` me hai.

**Day-1 pe `select` jaan-boojh kar choti hai** (`E9, F, B, S, UP, SIM, TCH, RUF`).
Jab tum koi file **touch** karte ho, us file mein ye rules clean hone chahiye.
Baaki repo baseline me hai — woh jaan-boojh kar chhoda gaya hai, galti nahi.

### Jo BANNED hai aur kyun

| Banned | Kyun |
|---|---|
| `# noqa` without comment | Repo me pehle se ~50 justified `# noqa: BLE001` hain (`app.py` ×21). Pattern wahi rakho — **suppress kar sakte ho, comment ke bina nahi.** |
| `# type: ignore` | Sirf 7 jagah hain, sab `[error-code]` form me. Naya ignore add karne se pehle samjho ki mypy kyun confuse hai. |
| `ruff format --check .` ko CI me daalna | Repo kabhi format nahi hua. Gate day-1 se permanently red hota hai aur team use delete kar deti hai. |
| Line-length / `E501` par argument | Cosmetic. Kabhi gate mat banao. |
| `C901 max-complexity = 10` | `chop_drop.py` / `chunking.py` ke bade procedural functions refactor karna = **behaviour risk**. Gate ka kaam bug rokna hai, code style thoon-na nahi. |

---

## 2. SECURITY — secrets kabhi source me nahi

Ye **pehle se do baar toota hai.** Details `SECURITY.md` me.

```python
# ❌ BANNED — kabhi bhi, kisi bhi reason se
API_HASH = "0c28170bc8f776097c5137411c1c248b"
app.save_tg_config("35578684", "real_hash_here", "+91928...", "@chan")
code = '46879'                      # OTP kabhi hardcode nahi

# ✅ CORRECT — env se
api_hash = os.environ["TDUBBER_API_HASH"]
```

**Ye galat soch hatao:** *"`config.json` DPAPI-encrypted hai, to safe hai."*
Nahi. DPAPI sirf **output** ko encrypt karta hai. Agar plaintext key source me
hai to encryption bekaar hai — wahi key code review, git history, aur har
`git clone` ke saath bahar chali gayi hai.

**Aur bhi banned:** `eval()`, `exec()`, `pickle` untrusted data pe,
`subprocess(shell=True)` jahan argv list ban sakti hai, SQL me f-string
(table name user input nahi hai to `# noqa: S608` + comment chahiye).

**Koi credential kabhi commit mat karo.** Aaya hai to turant `.gitignore`
me daalo, `git rm --cached` karo, aur **user ko rotate karne bolo** — ye
tera kaam nahi hai, ye uska hai.

---

## 3. TYPING — `mypy` strict on

`strict = true` globally. Naye code mein:

```python
def process(path: Path, *, retries: int = 3) -> list[Chunk]:
```

- `Any` avoid karo. Dict ke bajaye `dataclass` / `TypedDict` / `NamedTuple`.
- `Any` **zaroori** ho (untyped third-party lib) to **comment ke saath**:
  `x: Any  # noqa comment -- telethon ships no stubs`
- `Optional[X]` nahi, `X | None` (PEP 604).

**`tools/lint_gate.py --write-baseline` chalao** jab bhi koi typing fix karo —
warna baseline badhega aur gate permanently red ho jayega. **Baseline SHRINK
hona chahiye, kabhi grow nahi.**

---

## 4. TESTS — verify before you claim done

```bash
# 1. ruff     -- touched files only
ruff check <file.py>

# 2. mypy     -- touched files only
mypy <file.py>

# 3. pytest   -- FULL suite, always
python -m pytest
```

**"pytest chala liya" bolne se pehle output dekho.** Exit code 0 hona zaroori
hai, sirf print hone kaafi nahi.

### Test suite ke baare me jaano

- Root pe 14 test files hain (`pytest.ini` ke `testpaths`), `tests/` me sirf 5
  subtitle_forge wale. **`test_*.py` pattern root pe match karo, `tests/` pe nahi.**
- `conftest.py` ka autouse fixture **har test ko real `t_dubber.db` se protect
  karta hai.** Iska matlab: agar tumne woh bypass kiya, to production data gaya.
- 3 test jaan-boojh kar CI se bahar hain (`test_tg_cloud.py`, `test_upload.py`,
  `dashboard_contract_test.py`) — real server / real account chahiye. Unhe
  force-run mat karo.

### Run-on-import hazard — VERIFIED 2026-10-16

In files me **`if __name__ == "__main__"` guard nahi hai.** Matlab sirf
`import` karne se code chal jaata hai:

| File | Import karte hi kya hota hai |
|---|---|
| `remove_two_fixture_rows.py` | `db.connect()` + production DB me **`DELETE`** (backup + abort-guard hai, par chalta to hai) |
| `test_passwords.py` | `asyncio.run(main())` — **real Telegram session**, live credentials ke saath |
| `test_upload_manual.py` | **real Telegram upload** |
| `_probe_db.py` | read-only query + print. Harmless, bas noisy |

Do problems:

1. `test_passwords.py` aur `test_upload_manual.py` **`test_*.py` match karte
   hain.** `pytest.ini` ke `testpaths` ignore karte hue bare `pytest` chalaya,
   to woh **import hoke real Telegram call maarenge.** Hamesha repo root se
   `python -m pytest` chalao.

2. Isliye **kabhi aisa tool mat likho jo modules import karke inspect kare** —
   `ast.parse` use karo, ya subprocess chalao. `mypy` safe hai (execute nahi
   karta), lekin teri khud ka koi script unsafe hogi.

(`setup_config.py` me guard ab hai; `scratch_login.py` delete ho chuki hai.)

### Test library — `testkit.py` REUSE karo

MEASURED: 15 test files ne khud temp dir banaya, 10 ne sha256 likha, 7 ne wahi
fake-video line repeat ki, 3 ne HTTP handler khud likha. Sab `testkit.py` me
ab hai.

```python
# pytest me -- fixture pucho
def test_x(scratch_workspace, fake_movie, tgup_capture, sha256_of): ...

# standalone me -- direct banao (har test dono mode me chalta hai)
import testkit
ws = testkit.ScratchWorkspace()
```

**Naya helper kab banayein:** teesra caller tabhi. Do ek coincidence hai.
**Helper kabhi assert na kare** — assertion test me rahegi, warna failure ka
message library ko point karega, bug ko nahi.

Detail ke liye `CODE_MAP.md` section 6.

---

## 5. CI GATE — ratchet, cliff nahi

`tools/lint_gate.py` aaj ke findings ko baseline me freeze karta hai aur sirf
**NAYE** errors pe fail karta hai.

> **Gate day-1 pe GREEN hona chahiye. Jo gate red se shuru hota hai, wo delete
> ho jaata hai — aur fir kuch bhi protect nahi karta.**

### Measured numbers (2026-10-16, ruff 0.17.1 + mypy 2.4.0)

| | Raw findings | Baseline keys |
|---|---|---|
| ruff | 633 | 234 |
| mypy (`strict`) | 2358 lines (~1900 errors) | 520 |
| **Total baseline** | | **754** |

`mypy --strict` aaj **754** cheezein bolta hai — isiliye "sab files strict karo"
ek din me possible nahi tha, aur doosre AI ka plan Day-1 se red rehta.
Ratchet wahi kaam karta hai, bas har commit pe green rehta hai.

**90% cosmetic hai** (`UP006` List→list, `UP031` %-format→f-string,
`SIM105` try/except/pass, `RUF013` implicit Optional). **Asli bugs chhote hisse
me hain** aur inhe pehle fix karo:

| Rule | Count | Kya hai |
|---|---|---|
| `F811` | 13 | redefinition — asli conflict ho sakta hai |
| `F401` | 16 | unused import — dead weight / missing side-effect |
| `F841` | 4 | unused variable — bhool hua assignment |
| `B904` | 11 | `raise` without `from` — traceback kho jaata hai |
| `B023` | 1 | loop closure late-binding — classic bug |
| `S608` | 11 | SQL f-string — `db.py` me, prove karna padega |
| `S310` | 15 | `urlopen` — `link_resolver.py` SSRF surface |
| `S101` | 32 | **`assert` production code me** (`link_resolver.py` 17, `telegram_uploader.py` 11) — `python -O` se hat jaate hain |

### Ratchet kaise chalta hai

```bash
python tools/lint_gate.py                    # check. exit 1 = naya finding
python tools/lint_gate.py --write-baseline   # baseline chhota karo
```

**Har fix ke baad `--write-baseline` chalao.** Warna wo fix permanent
documented ho jayega aur kabhi shrink nahi hoga. `--write-baseline` batata
kitne **remove** hue ( asli fix) aur kitne **add** hue — add `0` hona chahiye.

### CI job alag hai

`build-native.yml` ka `python-tests` job **haath mat lagao.** Us file ke
comments me jaan-boojhkar likha hai:

> *"no pip install step exists here **on purpose** ... the test stopped being a
> free gate"* / *"this is **the one job worth blocking a merge on**"*

mypy/ruff ko wahan daalne se wo property khatam ho jayegi. Lint alag job hai:
`.github/workflows/lint-types.yml`.

---

## 6. AGENTS — merge se pehle review

`AGENTS_MAP.md` se pata karo kaun kis file pe kaam kar raha hai.

> **"Kisi agent ka code direct merge mat karo. Pehle topic samjho, review karo,
> phir merge."**

Merge se pehle ye confirm karo ki dusre ka change tera change delete to nahi kar
raha. Parallel agents ek hi file pe kabhi nahi — topic claim karo, likho, report
karo.

### Concurrency check — karo, warna kaam dubara hoga

MEASURED 2026-10-16: is repo pe ek hi waqt me **do agent** active the —
`telegram_uploader.py` aur `pipeline.py` dono minute ke andar badle rahe, jabki
koi unhe khaali nahi chhod raha tha.

**Kisi file ko touch karne se pehle:**

```powershell
git status --short <file>          # koi aur ne modify kiya?
Get-Item <file> | Select-Object LastWriteTime
```

Modified + aaj ka time = **koi usme abhi hai. Ruk ja, puch, phir likh.**

In-flight edit ko "theek" karna mat. Wo dobara likhne se purana kaab
overwrite ho jata hai. Gate ka koi naya finding bhi iska sign ho sakta hai —
matlab kisi ne abhi hi kuch add kiya hai jo clean nahi hai. Apna fix mat karo,
pehle report karo.

---

## 7. Definition of Done

Koi task "done" **tab** hai jab:

- [ ] File pehle check ki — koi aur usme nahi hai (section 6)
- [ ] `ruff check <touched files>` — 0 error
- [ ] `mypy <touched files>` — 0 error
- [ ] `python -m pytest` — **0 failed** (poora suite, output dekho, exit code dekho)
- [ ] Test likha ya badla — **bug fix bina test ke done nahi hai**
- [ ] Naya test `testkit.py` ka helper use kar raha hai, apna nahi banaya
- [ ] Naya secret introduce nahi kiya (`git diff | grep -iE 'hash|token|password|api_hash'`)
- [ ] Koi file/function delete ya move nahi kiya bina pooche
- [ ] `python tools/protocol_gate.py <touched files>` — exit 0, output dikhao (section 8)
- [ ] `python tools/lint_gate.py --write-baseline` — baseline **shrink** hua, grow nahi
- [ ] `AGENTS_MAP.md` me topic status update kiya

Ek bhi na ho to task complete **nahi** hai. Ye report karne se pehle check karo
— galat "done" report karna sabse buri cheez hai.

---

## 8. The Ironclad Protocol — har `.py` file pe, har agent ke liye

Ye section **binding** hai. `AGENTS_MAP.md` append-only coordination log hai;
rules sirf is file me rehte hain. Jab bhi tum koi naya `.py` file banaoge ya
existing file ko edit karoge, ye 4 steps bina kisi exception ke follow karo.

Protocol ka executable roop [`tools/protocol_gate.py`](tools/protocol_gate.py)
hai. Ye script inhe **mechanically** check karta hai — rules prose nahi hain
jo yaad rakhein, checks hain jo chalte hain. File likhne ke baad:

```bash
python tools/protocol_gate.py <file.py>
```

### Step 1 — Strict code standards (no shortcuts)

1. **Zero bare generics.** `dict`, `list`, `tuple` ko annotation me parameter
   ke bina nahi likhna. `dict[str, Any]`, `list[str]` likho. `-> dict` alone =
   violation.
2. **Explicit Optionals.** Default `None` ho to type `int` nahi balki
   `int | None` hona chahiye. `Optional[X]` nahi, `X | None` (PEP 604).
3. **Security (SQL & code injection).** SQL me f-string nahi — table/column
   names ke liye `.isidentifier()` validation ya parameterized queries.
   `eval()`, `exec()`, `subprocess(shell=True)` banned. (Ruff ke `S` rules ye
   bhi pakadte hain.)
4. **Helper assertions ban.** Shared helper libraries (`testkit.py` jaisi) ke
   andar `assert` nahi. Asserts test files me rahte hain. Helper me check karna
   ho to `raise ValueError` / `raise AssertionError` do. (Jahan assert hi helper
   ka kaam hai, wahan `# noqa: S101` + comment — `tg_link_roundtrip` ka
   assert documented exception hai.)

### Step 2 — The mandatory terminal loop

File likhne/edit karne ke baad, output tab tak user ko **nahi** doge jab tak
tum khud `protocol_gate.py` chalake exit code 0 verify nahi kar lete. Exit 1
matlab koi naya finding hai — fix karo.

### Step 3 — `__all__` sync

Nayi module/utility file banani ho to shuru me `__all__` list zaroor do. Public
function/class/constant add karte hi `__all__` me naam add karo. Existing file
me `__all__` hai to wo sync me hona chahiye — koi public name miss nahi hona
chahiye. (Measured: `testkit.py` me `free_port`, `stop_server`, `write_json`
missing the — gate ne pakde, fix ho chuke hain.)

### Step 4 — Output format

Gate pass hone par, response me terminal ka output dikhao jisme saaf likha ho:

```
  [ruff] All checks passed!
  [mypy] Success: no issues found in 1 source file(s)
  [ast]  clean

Gate: PASS. No new findings. Protocol satisfied.
```

Bina terminal verification ke code output nahi diya jaayega.