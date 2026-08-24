"""
One-off backfill: turn legacy SYNC_FROM_T212 prose entries into the structured
SYNC_RESET / SYNC_ADD / SYNC_REMOVE records that compute_realized_pnl() replays.

Before August 2026 sync recorded only a human-readable note ("removed ['META']"),
so the realised-P&L replay could not see that a position had been withdrawn.
Phantom lots stayed in the replay forever: the four April 2026 META buys that
T212 rejected blended into the cost basis of the real 13 May position and turned
a -£48.99 loss into a reported -£93.79.

sync_from_t212() writes these records itself from now on. This script fixes the
history that predates it. It is idempotent — re-running changes nothing.

    python migrate_trade_log.py            # dry run, prints the diff
    python migrate_trade_log.py --apply    # writes, after backing up the ledger
"""

import json
import re
import shutil
import sys
from datetime import datetime

LEDGER = "shadow_portfolio.json"

RE_REBUILT = re.compile(r"Shadow rebuilt from T212")
RE_BIDI    = re.compile(r"added \[(?P<added>[^\]]*)\], removed \[(?P<removed>[^\]]*)\]")
RE_TICKER  = re.compile(r"'([^']+)'")


def build_records(trades: list) -> list:
    """Return a new trade list with structured sync records inserted."""
    out = []
    # Drop records a previous run inserted, then rebuild them — otherwise
    # re-running duplicates every SYNC_RESET and SYNC_REMOVE.
    trades = [
        t for t in trades
        if not str(t.get("note", "")).startswith("backfilled")
    ]
    for t in trades:
        if t.get("action") != "SYNC_FROM_T212":
            out.append(t)
            continue

        note = t.get("note") or ""
        date = t.get("date")

        if RE_REBUILT.search(note):
            # Ledger was replaced wholesale by T212 state; every lot recorded
            # before this point is gone and its basis was never logged.
            out.append({
                "date":   date,
                "action": "SYNC_RESET",
                "ticker": "-",
                "note":   f"backfilled from legacy sync entry: {note}",
            })
            out.append(t)
            continue

        m = RE_BIDI.search(note)
        if m:
            for tk in RE_TICKER.findall(m.group("removed")):
                out.append({
                    "date":   date,
                    "action": "SYNC_REMOVE",
                    "ticker": tk,
                    "note":   "backfilled: not held at T212 — order rejected or never executed",
                })
            for tk in RE_TICKER.findall(m.group("added")):
                # Legacy notes never recorded share count or cost for adds, so
                # the basis is genuinely unrecoverable — flag it rather than
                # inventing a number.
                out.append({
                    "date":   date,
                    "action": "SYNC_ADD",
                    "ticker": tk,
                    "shares": 0,
                    "note":   "backfilled: shares/cost not recorded by legacy sync",
                })
        out.append(t)
    return out


def stamp_baseline(ledger: dict) -> bool:
    """
    Record today's positions and cash as the reconciliation starting point.

    Shares held before the April 2026 rebuilds were never written to the trade
    log, so reconcile_trade_log() replaying from £5,000 can never balance. The
    baseline says "this is what was really held here" so any drift after it is
    a genuine bug rather than inherited bootstrap noise.
    """
    trades = ledger["trades"]
    if any(t.get("action") == "SYNC_BASELINE" for t in trades):
        return False
    trades.append({
        "date":      datetime.now().strftime("%Y-%m-%d"),
        "action":    "SYNC_BASELINE",
        "ticker":    "-",
        "positions": {
            tk: p.get("shares", 0) for tk, p in ledger.get("positions", {}).items()
        },
        "cash_gbp":  ledger.get("cash_gbp", 0),
        "note":      "reconciliation baseline — pre-April-2026 share history not in log",
    })
    return True


def main() -> int:
    apply = "--apply" in sys.argv

    with open(LEDGER, encoding="utf-8") as f:
        ledger = json.load(f)

    before = ledger["trades"]
    after  = build_records(before)

    needs_baseline = not any(t.get("action") == "SYNC_BASELINE" for t in before)

    if after == before and not needs_baseline:
        print("Already up to date — trade log carries structured sync records.")
        return 0

    new = [t for t in after if str(t.get("note", "")).startswith("backfilled")]
    if new:
        print(f"{len(new)} record(s) to insert:\n")
        for t in new:
            print(f"  {t['date']}  {t['action']:12s} {t['ticker']}")
    if needs_baseline:
        print("\n  + SYNC_BASELINE (reconciliation starting point)")

    import shadow_portfolio as sp

    for label, trades in (("BEFORE", before), ("AFTER", after)):
        probe = dict(ledger)
        probe["trades"] = trades
        pnl = sp.compute_realized_pnl(probe)
        print(f"\n=== {label} ===")
        print(f"  realised total: £{pnl['total_gbp']}")
        for tk, v in pnl["by_ticker"].items():
            print(f"    {tk:6s} £{v}")
        print(f"  no basis:       {pnl['tickers_with_incomplete_basis']}")
        print(f"  estimated:      {pnl['tickers_with_estimated_basis']}")

    if not apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{LEDGER}.bak_{stamp}"
    shutil.copy2(LEDGER, backup)
    ledger["trades"] = after
    stamp_baseline(ledger)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)

    recon = sp.reconcile_trade_log(ledger)
    print(f"\nReconciliation: {'clean' if recon['clean'] else recon['drifts']}")
    print(f"  cash: log £{recon['cash_log_gbp']} vs ledger £{recon['cash_ledger_gbp']}")
    print(f"\nWritten. Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
