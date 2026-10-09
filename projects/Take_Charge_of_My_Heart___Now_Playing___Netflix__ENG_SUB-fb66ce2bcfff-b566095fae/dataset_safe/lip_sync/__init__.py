"""Lip-sync providers for T_Dubber (MuseTalk / Wav2Lip)."""

from .lip_sync import (  # noqa: F401
    FakeProvider,
    LipSyncProvider,
    LipSyncResult,
    MuseTalkProvider,
    Wav2LipProvider,
    build_provider,
)
