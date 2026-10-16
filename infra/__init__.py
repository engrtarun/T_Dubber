"""Declarative runtime wiring for T_Dubber, in HCL.

The manifest lives in ``manifest.auto.tfvars``. Terraform can read it as-is the
day this project has a provider to talk to; ``infra.hcl`` reads it today,
without terraform installed. See ``infra/README.md`` for why the two are not
the same thing yet.
"""

from __future__ import annotations

__all__: list[str] = []
