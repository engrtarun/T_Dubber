# terraform block: the version floor, and nothing else.
#
# There is no `required_providers` block yet and that is deliberate. Adding one
# without a resource to use it makes `terraform init` download a provider that
# then has nothing to talk to, which reads as infrastructure management while
# managing none. The block is added in the same commit as the first resource --
# see infra/README.md, "when Terraform starts being real".
terraform {
  required_version = ">= 1.6.0"

  # 1.6 is the floor because `terraform validate` on a directory with no
  # backend and no state should never need to touch the network. An older
  # binary tries to initialise a backend it does not have.
}