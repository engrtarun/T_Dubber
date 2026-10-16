# SECURITY.md — T_Dubber

## Incident record: Telegram credentials leaked to git history

**Status:** source fixed. **Rotation: still required by the account owner.**

### What was exposed

| Item | Where | Status |
|---|---|---|
| Telegram `api_hash` | `scratch_login.py:5`, `setup_config.py:9`, `test_upload_manual.py:17` | removed from working tree |
| Phone number `+919286175802` | `setup_config.py:9`, `DIRECT_LINK_TG_UPLOAD.md:631` | removed from working tree |
| Login OTP | `scratch_login.py:7` | file deleted |
| Compiled bytecode copy of the hash | `__pycache__/test_upload_manual.cpython-314.pyc` | untracked |

### Commits that contain the plaintext `api_hash`

```
45af352  ddd99f9  f117a5d  198962f  4efa7b4  b503b26
```

Verified with `git log -S` against the full history and all refs.

### Why the DPAPI encryption did not help

`config.json` stores `api_hash_protected`, encrypted with Windows DPAPI
(`app._protect_api_hash`). That control was real but worthless in practice,
because the **plaintext key sat in the source that feeds it**.

A secret hardcoded in a tracked file is a *published* secret. Encrypting a
copy of it in a config file changes nothing for anyone who can run
`git clone`.

### Actions still required (account owner only — cannot be done from the repo)

1. **Revoke and regenerate `api_hash`** at my.telegram.org.
2. **Terminate all other sessions** for the phone number (Settings → Devices),
   because a `.session` auth key may also have been exposed historically.
3. If the repo is or ever was a public GitHub repository, treat both as
   compromised regardless of the code cleanup.

### What was done in the repo

- `scratch_login.py` deleted. It was dead anyway: it imported
  `tg_up.client.tg_upload_client`, and no `tg_up` package exists in this repo,
  so it could never have run.
- `setup_config.py` rewritten to read `TDUBBER_API_ID` / `TDUBBER_API_HASH` /
  `TDUBBER_PHONE` / `TDUBBER_CHANNEL` from the environment, and to refuse to
  run with a message naming the missing variables instead of silently
  proceeding.
- `test_upload_manual.py` now delegates to `app.load_tg_config()` instead of
  short-circuiting DPAPI with a literal. It previously carried the comment
  *"Hardcoded just for test since DPAPI takes time to import/setup in script"* —
  which was the bug, not the justification.
- 20 `.pyc` files untracked. `.gitignore` already listed `__pycache__/` and
  `*.pyc`, but those files were committed long before the rule, and gitignore
  has no effect on already-tracked paths. One of them carried the api_hash.
- The deleted login OTP is gone. An OTP is single-use and long expired; there
  is nothing to revoke.

**History was not rewritten.** `git log -S` still returns those commits. A
history rewrite would invalidate every clone and every open branch, so it is a
decision for the repository owner — and it is pointless for `api_hash`
specifically, because rotating the credential already renders the old value
useless.

---

## Rules

### Secrets

Credentials come from the environment. Never from a literal in a tracked file.

```
TDUBBER_API_ID  TDUBBER_API_HASH  TDUBBER_PHONE  TDUBBER_CHANNEL
```

`config.json` is the intended *runtime* store and DPAPI-protects the hash.
Source code is not a store.

Before every commit:

```bash
git diff | grep -inE 'api_hash|api_id|password|token|secret|otp|code\s*='
```

If that matches, stop. Do not commit. See `AGENTS.md` section 2.

### Committed credential material

| Never commit | Why |
|---|---|
| `*.session`, `tgup.session`, `telegram_uploader_session.session` | A Telethon/gotd session holds the account **auth key**. Anyone with the file can connect as the account — no OTP, no password. |
| `config.json` plaintext `api_hash` | Read `app.load_tg_config()` instead; it migrates the plaintext to DPAPI on first read. |
| `pwd.txt`, `qr.png`, `*.jpg` screenshots | Login tokens and API screenshots. |
| `__pycache__/`, `*.pyc` | Compiled bytecode. They are stale by definition, and they are a credential leak vector — see the incident above. |

### Code

- No `eval()`, `exec()`, `pickle` on untrusted data.
- No `subprocess(..., shell=True)` where an argv list would do.
  `tgup_bridge.py` and `go_planner.py` invoke `tgup.exe` through `subprocess`
  **by design** — that is the Go fast path, and it is deliberately exempted
  from `S603`/`S607` in `pyproject.toml`. Do not "fix" it by shelling out
  through a shell.
- SQL built by string interpolation needs `# noqa: S608` plus a comment proving
  the identifier cannot be user-controlled.
- `link_resolver.py` already implements an SSRF allowlist
  (`assert_fetchable_url`). Do not weaken it.

### Reporting

Report privately to the repository owner. Do not open a public issue
containing a live credential, even one you believe is already revoked.