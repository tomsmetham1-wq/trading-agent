"""
One-off backfill of DELL's played-out declaration price.

position_capture() scores the one discretionary decision in a realized
winner's history - keeping the remainder rather than exiting the day the
thesis was declared played out - against the price on that day. From
Sep 2026 record_played_out_prices() stores that price at declaration
(`played_out_price_gbp`, basis "actual"). DELL was declared on 2026-07-27,
before the field existed, and nothing in the ledger holds the price:
SET_DRIVER is ledger-only so no fill reached the trade log, and the weekly
snapshot stores ticker names only.

The 2026-07-27 report declared the thesis "definitively played out" at
+97.6% from entry (recorded in CLAUDE.md and the commit that added
SET_DRIVER). Entry is GBP 159.7251, so the declaration price is
159.7251 * 1.976 = GBP 315.62. Stored with basis "backfilled" so every
rendering says so; the number is to a few pounds, not exact.

Idempotent. Run with --apply to write; backs the ledger up first.
"""

import json
import shutil
import sys
from datetime import datetime

LEDGER        = "shadow_portfolio.json"
TICKER        = "DELL"
DECLARED      = "2026-07-27"
GAIN_AT_DECL  = 0.976          # +97.6% from entry, per the 27 Jul 2026 report


def repair(ledger: dict) -> list[str]:
    pos = (ledger.get("positions") or {}).get(TICKER)
    if pos is None:
        return []
    if pos.get("played_out_price_gbp") is not None:
        return []
    history = pos.get("forward_driver_history") or []
    if not history or history[0].get("date") != DECLARED:
        return []
    entry = float(pos.get("avg_cost_gbp") or 0)
    if entry <= 0:
        return []
    price = round(entry * (1 + GAIN_AT_DECL), 4)
    pos["played_out_price_gbp"] = price
    pos["played_out_price_basis"] = "backfilled"
    return [f"{TICKER}: played-out declaration price backfilled at GBP {price:.2f} "
            f"(+{GAIN_AT_DECL * 100:.1f}% on entry GBP {entry:.4f}, {DECLARED})"]


def main() -> int:
    with open(LEDGER, encoding="utf-8") as f:
        ledger = json.load(f)

    done = repair(ledger)
    if not done:
        print("Nothing to repair — record already present.")
        return 0

    for line in done:
        print(f"  {line}")

    if "--apply" not in sys.argv:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    backup = f"{LEDGER}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(LEDGER, backup)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    print(f"\nWritten. Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
