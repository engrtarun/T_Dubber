"""P1 concurrency bench — saved credentials + phone, ek real terminal se chalao.

Nimbu ka banaya hua wrapper. Ye `bench_p0.py --sections p1` ka wahi kaam karta
hai.

Session ka rasta ab yun hai: `tgup_bridge.bench()` shuru me
`ensure_session()` chalata hai, jo Python wala login (`telegram_uploader_session.session`)
agar authorized ho toh uski auth key `tgup.session` me utar deta hai — bina OTP
ke. Agar Python wala session bhi dead ho (Telegram ne revoke kiya ho), tabhi
login chahiye, aur tab ye script ek real terminal maangti hai, kyunki code ka
jawab sirf wahi de sakta hai.

    python login_once.py       # ek baar login + Go session dono (pehle chalao)
    python bench_p1_tty.py

Exit code 0 = bench chala, results `bench_p0_results_log.jsonl` me bhi jude.
Exit code 1 = wahi auth error, jo NOVA ka block tha.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# tgup ek OTP bhejta hai jisko answer karne ke liye terminal chahiye. Ye flag
# sirf tab chahiye jab session file bilkul na ho; yahan file hai par wo
# stale hai, isliye iska hona zaroori nahi — par hona zaroori hai taaki
# bridge khud mana na kare.
os.environ.setdefault("TGUP_ALLOW_LOGIN", "1")

import app  # noqa: E402
import tgup_bridge  # noqa: E402

RESULTS_LOG = ROOT / "bench_p0_results_log.jsonl"


def append_history(section: str, payload: dict) -> None:
    """Har run ko alag line me rakho.

    `bench_p0_results.json` har run par **overwrite** hota hai (uska apna
    design), isliye wahan pichhla result bachta nahi. Ye log wahi kachra rokta
    hai.
    """
    entry = {
        "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "section": section,
        "run_by": "nimbu/bench_p1_tty",
    }
    entry.update(payload)
    with RESULTS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    api_id, api_hash, phone, channel = app.load_tg_config()
    if not (api_id and api_hash and phone and channel):
        print("config.json incomplete: api_id/api_hash/phone/channel chahiye", file=sys.stderr)
        return 2

    print(f"channel     : {channel}")
    print(f"phone       : {phone[:5]}****{phone[-2:]}")
    print("session     :", tgup_bridge.session_path())
    print("concurrency : 1,2,3,4   (48 MB payload per level)")
    print("\nAgar Telegram ne login code bheja hai toh yahin type karo.\n", flush=True)

    report = tgup_bridge.bench(
        channel=channel,
        api_id=api_id,
        api_hash=api_hash,
        phone=phone,
        size_mb=48,
        levels="1,2,3,4",
        on_human=lambda line: print(line, flush=True),
    )
    print(json.dumps(report, indent=2, default=str))
    append_history("p1", {"ok": bool(report.get("ok")), "report": report})
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
