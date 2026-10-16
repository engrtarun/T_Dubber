# Outputs exist so that `terraform output` can answer the one question this
# directory is really about: is the wiring declared here the wiring in use?
#
# They read the manifest and nothing else. No resource is created, nothing is
# reconciled, and no provider is contacted -- so if `terraform output` and
# `python -m infra plan` ever disagree, the disagreement is in one of those two
# readers, not in the state of the world. That is the property worth having.

output "telegram_channel" {
  description = "The one channel name, declared once."
  value       = var.telegram.channel
}

output "session_hops" {
  description = "Every place that needs a Telegram session, in hop order."
  value = {
    telethon   = var.telegram.telethon_session
    local_gotd = var.telegram.local_session
    kaggle     = var.kaggle.session_path
  }
}

output "kaggle_dataset" {
  description = "The private dataset that carries the auth key to hop 3."
  # dataset_slug and isPrivate are quoted as a pair on purpose. Printing the
  # slug alone would let it read like a harmless name, and it is not: the slug
  # addresses the artifact that holds the account's auth key.
  value = {
    slug    = var.kaggle.dataset_slug
    private = var.kaggle.private
  }
}

output "edge_url" {
  description = "Optional edge Space. Empty string means not configured."
  value       = var.edge.url
}