# =============================================================================
#  manifest.auto.tfvars -- the single source of truth for runtime wiring
# =============================================================================
#  WHY THIS FILE IS HCL AND NOT YAML/JSON/TOML
#  -------------------------------------------
#  Because the day this project gets a real provider -- a VPS for the Telegram
#  relay that DIRECT_LINK_TG_UPLOAD.md section A2 needs -- this file is read by
#  terraform unchanged, in the directory next to variables.tf. Choosing HCL now
#  costs nothing; choosing YAML now and HCL later means two files to keep in
#  step, and a config that has drifted from the code is the exact failure this
#  whole directory exists to prevent.
#
#  IT IS NOT TERRAFORM APPLYING ANYTHING YET. There is no provider block, no
#  resource, no backend. `terraform validate` passes and that is all it does.
#  Pretending otherwise would be the worst outcome available: a green plan
#  output that changed nothing.
#
#  NO SECRETS IN THIS FILE. Ever.
#  The auth key, the api_hash and the phone number do not belong here -- a file
#  tracked in git is a published file (SECURITY.md, incident record). This
#  manifest names *paths and shapes*; the values live in the environment.
#
#  Kept deliberately narrow: no interpolation, no functions, no for-expressions,
#  because infra/hcl.py refuses all three and a manifest that only terraform can
#  read is a manifest that cannot be read at the moment you need it -- on the
#  workstation, pre-flight, before a kernel starts.
# =============================================================================

telegram = {
  # THE channel name, declared once.
  #
  # It currently has three historical spellings across the repo: $TDUBBER_CHANNEL,
  # $TDUBBER_TG_CHANNEL and $TDUBBER_TGUP_CREDS. That is not three settings,
  # it is one setting with three names, and it is why rotating anything has
  # meant hunting through four files. One key here; the aliases are a
  # compatibility problem, not a configuration one.
  channel = ""

  # Hop 1 -- the Python (Telethon) login. Written by `login_once.py` after one
  # interactive OTP, and then never written again. 256 bytes plus a DC id.
  telethon_session = "telegram_uploader_session.session"

  # Hop 2 -- the local Go `tgup`. NOT logged in: telethon_session.py copies the
  # auth key across from hop 1, which is the whole reason the Python -> Go move
  # cost one OTP instead of a second one.
  local_session = "tgup.session"
}

kaggle = {
  # The private dataset that carries the auth key to hop 3.
  #
  # READ THIS BEFORE CHANGING `private` TO false.
  # An MTProto auth key IS the account: SECURITY.md is explicit that anyone
  # holding this file can connect as you, with no OTP and no password. This
  # dataset is therefore the most sensitive thing in the repository, and it is
  # the one artifact here that leaves your machine.
  dataset_slug = ""

  private = true

  # Hop 3 -- the Go binary on the Kaggle worker.
  #
  # Absolute on purpose. tgup_bridge.py and the Dockerfile ENV both point here;
  # a relative path would resolve against whatever directory the kernel happened
  # to start in, which is how the binary ends up "finding no session" and
  # deciding it must log in -- an OTP spent, and a machine with no terminal to
  # type into can only end in EOF.
  session_path = "/kaggle/working/tgup.session"
}

edge = {
  # The optional edge Space (edge/, Go) that serves pylibs and weights 24/7.
  # Empty means "not configured": hf_store.py falls through to the hub, which is
  # slower but correct. See huggingface/.env.example for TDUBBER_EDGE_URL.
  url = ""
}