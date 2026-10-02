"""mazinger profile — generate or list voice profiles from themes."""

from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "profile",
        help="Generate a reusable voice profile from a theme, or list available themes.",
    )
    sub = p.add_subparsers(dest="profile_action", required=True)

    # mazinger profile list
    sub.add_parser("list", help="List available voice themes.")

    # mazinger profile generate
    gen = sub.add_parser("generate", help="Generate a voice profile from a theme.")
    gen.add_argument("theme", help="Voice theme name (e.g. 'narrator-m', 'young-f').")
    gen.add_argument(
        "language",
        help="Target language (e.g. 'English', 'Spanish').",
    )
    gen.add_argument(
        "-o", "--output", required=True,
        help="Output directory for the profile (will contain voice.wav + script.txt).",
    )
    gen.add_argument("--device", default="auto", help="Device: auto (default), cuda, or cpu.")
    gen.add_argument("--dtype", default="bfloat16", help="Weight dtype for Qwen VoiceDesign model.")
    gen.add_argument("-v", "--verbose", action="store_true", help="Enable debug-level logging.")


def handler(args: argparse.Namespace) -> None:
    if args.profile_action == "list":
        _handle_list()
    else:
        _handle_generate(args)


def _handle_list() -> None:
    from mazinger.profiles import list_themes
    themes = list_themes()
    males = [t for t in themes if t["gender"] == "male"]
    females = [t for t in themes if t["gender"] == "female"]
    print(f"Available voice themes ({len(themes)} total: {len(males)}M / {len(females)}F):\n")
    for t in themes:
        langs = ", ".join(t["languages"])
        print(f"  {t['name']:<16s} {t['gender']:<7s} [{langs}]")


def _handle_generate(args: argparse.Namespace) -> None:
    from mazinger.cli._groups import resolve_device
    from mazinger.profiles import generate_profile
    from mazinger.tts import validate_language

    validate_language(args.language)
    args.device = resolve_device(args.device)

    device = args.device.split(":")[0] + ":0" if ":" not in args.device else args.device
    voice_wav, script_txt = generate_profile(
        args.theme, args.language, args.output,
        device=device, dtype=args.dtype,
    )
    print(f"Profile generated in {args.output}/")
    print(f"  voice:  {voice_wav}")
    print(f"  script: {script_txt}")
    print(f"\nUsage: mazinger dub <source> --clone-profile {args.output}")
