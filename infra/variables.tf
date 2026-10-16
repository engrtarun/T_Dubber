# Typed declarations for the three objects in manifest.auto.tfvars.
#
# These exist so that a typo in the manifest is a `terraform validate` error
# rather than a silent empty string discovered three weeks later on a kernel.
# object({...}) is used rather than map(string) on purpose: map(string) would
# accept `private = "false"` and store the string "false", which is truthy --
# and this particular flag decides whether an account's auth key is world
# readable, so "truthy in the wrong direction" is not a cheap mistake.

variable "telegram" {
  description = "Telegram account wiring: one channel, two session files."
  type = object({
    channel           = string
    telethon_session  = string
    local_session     = string
  })
}

variable "kaggle" {
  description = "How the auth key reaches the Kaggle worker."
  type = object({
    dataset_slug = string
    private      = bool
    session_path = string
  })
}

variable "edge" {
  description = "Optional edge Space URL. Empty string means not configured."
  type = object({
    url = string
  })
}