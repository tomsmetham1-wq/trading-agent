"""
One-off data repair for two watchlist records written on 2026-08-24.

  META  sold that run, and the report said "the watchlist is the right place
        for that" — but the name was never added, so the exit is scored
        nowhere. record_watchlist() now tracks exits automatically; this
        backfills the one it already missed.

  MSFT  recorded as "blocked on AI infrastructure theme concentration". The
        buy would have taken the theme to 55.0% against a 60% cap and the
        guard log shows no block — it was a discretionary preference for
        diversification. Left alone, the record would explain a missed idea
        with a constraint that never existed.

Idempotent. Run with --apply to write; backs the ledger up first.
"""

import json
import shutil
import sys
from datetime import datetime

LEDGER   = "shadow_portfolio.json"
RUN_DATE = "2026-08-24"

MSFT_THESIS = (
    "Azure AI growing 50%+ with Copilot enterprise penetration in early "
    "innings at ~25x forward P/E; not bought this week — a new position would "
    "take AI infrastructure to ~55% against a 60% cap, so Visa was preferred "
    "for diversification. A choice, not a guard block."
)


def repair(ledger: dict) -> list[str]:
    watchlist = ledger.setdefault("watchlist", {})
    done: list[str] = []

    exit_trade = next(
        (t for t in ledger.get("trades", [])
         if t.get("ticker") == "META" and t.get("closed_position")),
        None,
    )
    if exit_trade:
        sold_on = exit_trade.get("date", RUN_DATE)
        rec = watchlist.get("META")
        if rec is None:
            rec = watchlist["META"] = {
                "first_seen":      sold_on,
                "yfinance_ticker": "META",
                "observations":    [],
                "active":          False,
                "source":          "exit",
                "exited_on":       sold_on,
                "last_seen":       sold_on,
                "theme":           "AI infrastructure",
                "thesis":          (exit_trade.get("exit_thesis") or "").strip(),
            }
            done.append("META: added as a tracked exit")

        # Seed the first observation from the sale itself. Scoring an exit
        # means measuring from the price it was sold at, and this repair runs
        # outside a weekly run, so nothing else would price it until the
        # following Monday — losing a week of the comparison.
        sale_price = exit_trade.get("price_gbp")
        if sale_price and not rec.get("observations"):
            snapshots = ledger.get("weekly_snapshots") or []
            benchmark = next(
                (s.get("benchmark_return_pct") for s in reversed(snapshots)
                 if s.get("date") == sold_on),
                None,
            )
            observation = {"date": sold_on, "price_gbp": round(float(sale_price), 4)}
            if benchmark is not None:
                observation["benchmark_return_pct"] = benchmark
            rec["observations"] = [observation]
            done.append(
                f"META: first observation seeded at the sale price "
                f"(£{sale_price:.2f} on {sold_on})"
            )

    msft = watchlist.get("MSFT")
    if msft and "blocked" in (msft.get("thesis") or "").lower():
        msft["thesis"] = MSFT_THESIS
        done.append("MSFT: thesis reworded — the buy was a choice, not a block")

    return done


def main() -> int:
    with open(LEDGER, encoding="utf-8") as f:
        ledger = json.load(f)

    done = repair(ledger)
    if not done:
        print("Nothing to repair — both records already correct.")
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
