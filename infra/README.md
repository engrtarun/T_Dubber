# infra/ — runtime wiring, declared once

## What this is

One HCL file, `manifest.auto.tfvars`, that says where every Telegram session
lives and which one carries which account. `infra.hcl` reads it without
terraform; terraform can read it unchanged the day there is something for it to
do.

## Why HCL, and why not terraform yet

Terraform manages cloud resources **through a provider** — a plugin that talks
to one vendor's API. This project has three moving parts and none of them has
one:

| Component | Who runs it | Terraform provider |
|---|---|---|
| Telegram (storage, MTProto sessions) | Telegram's own DC farm | none — Bot API is HTTP, not a provider |
| Kaggle GPU kernels | Kaggle | none — REST API + `kaggle` CLI, no provider |
| `edge/` Space (Go) | Hugging Face Spaces | community provider exists; reality is GH Actions + `docker push` + `hf` CLI |
| `t_dubber.db` (SQLite) | this machine | none — it is a file, not a resource |

So `terraform apply` here would manage nothing. Writing `.tf` files that *look*
like infrastructure management while changing no infrastructure is the worst
available outcome: a green plan that proves nothing, read by someone who
believes it proves something.

What HCL buys without terraform is the part that was actually broken — see
below. The provider arrives with the first real resource, in the same commit,
which is when `terraform` starts being true rather than decorative.

## The bug this exists to fix

Three hops touch Telegram, and each used to learn its session path by a
different mechanism:

1. **Python/Telethon** — `telegram_uploader_session.session`, beside the repo,
   overridable with `$TGUP_TELETHON_SESSION`
2. **local Go `tgup`** — `tgup.session`, overridable with `$TGUP_SESSION`
3. **Kaggle worker** — `/kaggle/working/tgup.session`, written as an `ENV` line
   in the Dockerfile and nowhere else

Hop 2 was solved once, properly: `telethon_session.py` copies the 256-byte
MTProto auth key into gotd's format instead of asking Telegram for a new one.
One OTP, total.

**Hop 3 was never given that treatment.** Nothing puts the key on the kernel, so
the Go binary finds no session, decides it must log in, dispatches a login code
the moment the auth flow starts, and a machine with no terminal to type into can
only end in EOF. `Dockerfile:536` documents this in as many words, eleven lines
above the `ENV` that causes it.

Run the tool and it says so out loud:

```
[info ] telethon:   ok   carries auth key id 9J6ENvxaVOs=
[info ] local_gotd: ok   carries auth key id 9J6ENvxaVOs=
[error] kaggle:     missing  no session file at /kaggle/working/tgup.session
                    fix: run: python -m infra push
```

Two hops agree on one account. The third is simply absent.

## Using it

```powershell
python -m infra           # plan: compare declared wiring with this machine
python -m infra push      # stage + publish hop 3's session dataset (private)
python -m infra push --create   # first push, when the dataset does not exist
```

Exit status is 1 when any finding is an error, so it can sit in a pre-flight
hook without anyone reading the output to know whether to continue.

## The finding that matters most

Every hop's path has a declared value *and* a live environment variable, and
the environment wins at run time — because the Dockerfile sets `TGUP_SESSION`
and `tgup_bridge.py` honours it. When the two disagree, the manifest is the
thing that has stopped being true. `check()` reports that as `diverges`.

That is precisely the hop-3 bug in its general form, and it is the class of
fault this whole directory exists to surface before a kernel starts rather than
twenty minutes into a run.

## Security posture

- **No secrets in the manifest.** A file tracked in git is a published file
  (`SECURITY.md`, incident record). The manifest names paths and shapes; values
  live in the environment.
- **`infra/_staging/` is gitignored.** It holds a copy of the authorised
  session between staging and the CLI call.
- **`push` refuses a non-private dataset.** An MTProto auth key *is* the
  account: anyone holding the file can connect with no OTP and no password. So
  `private = true` is checked in code, not left as a comment.
- **The key id is printed in full, on purpose.** It is eight bytes of the key's
  SHA-1, so it is an identifier, not a key — and "same id" is exactly the claim
  the report makes. The 256-byte key never appears anywhere.

## When Terraform starts being real

The trigger is a VPS. `DIRECT_LINK_TG_UPLOAD.md` section A2 (server-side fetch
via `inputMediaDocumentExternal`) needs a host, and its own notes say so. At
that point, in one commit:

- a `provider` block, and a `required_providers` entry to go with it
- `resource "hcloud_server" "relay"` (or DO/Oracle equivalent)
- `outputs.tf` gains the relay URL that `edge.url` already has a slot for

Nothing here has to move. The manifest becomes the tfvars those variables
declare, and `variables.tf` already has the types.

## Files

| File | Role |
|---|---|
| `manifest.auto.tfvars` | the single source of truth. HCL, no secrets |
| `hcl.py` | the HCL2 subset reader. Refuses interpolation, heredocs, functions |
| `manifest.py` | the three hops, key-id comparison, `check()` |
| `publish.py` | hop 3: stage + push the session as a private Kaggle dataset |
| `__main__.py` | `python -m infra` |
| `versions.tf` / `variables.tf` / `outputs.tf` | real terraform, currently inert |

Tests: `test_infra_manifest.py` at the repo root (both `pytest` and standalone).