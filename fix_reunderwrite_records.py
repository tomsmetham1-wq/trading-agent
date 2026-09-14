"""
One-off data repair for the AMZN and GOOGL records written on 2026-09-14.

The prompt asked for both positions' backfilled entry theses to be
re-underwritten "as a SET_DRIVER". SET_DRIVER is the played-out mechanism:
_apply_set_driver() sets thesis_played_out and _inject_played_out_banks()
then trimmed a third of each — AMZN at -3.6%, GOOGL at +0.16% — "to convert
paper alpha into realised alpha" on positions that had none. The trims are
done at T212 and stay done (the flip-flop guard rightly stops a same-week
rebuy). What this repairs is the state they left behind:

  positions  Both are marked played out for good (the flag is never cleared
             by design), so they sit on the 12-week bank drip, the driver-count
             escalation (#3 banks, #4 exits), the giveback logic and the
             "trim within 15% of today" rule — all the realized-winner
             machinery, on two flat positions. The re-underwritten case moves
             into `thesis` where it belongs, stamped "[Re-underwritten
             2026-09-14]" so provenance scores it from that date, and every
             played-out field is removed.

  trades     The two SET_DRIVER records become SET_THESIS, keeping the
             backfilled text they replaced. The two TRIM records get an
             exit_thesis that says what actually happened, so the "recent
             exits" replay does not present them as a thesis judgement, with
             the generated text kept under exit_thesis_original.

Idempotent. Run with --apply to write; backs the ledger up first.
"""

import json
import shutil
import sys
from datetime import datetime

LEDGER   = "shadow_portfolio.json"
RUN_DATE = "2026-09-14"
TICKERS  = ("AMZN", "GOOGL")

TRIM_EXIT_THESIS = (
    "Process defect, not a decision: the 2026-09-14 re-underwrite was filed "
    "as SET_DRIVER, which declared the thesis played out and triggered the "
    "mechanical 33% bank on a position with no gain to bank. The "
    "re-underwritten thesis is intact and this trim is not evidence about it. "
    "SET_THESIS now exists for re-underwriting; SET_DRIVER is for realized "
    "winners only."
)

PLAYED_OUT_FIELDS = (
    "thesis_played_out", "forward_driver", "forward_driver_set",
    "forward_driver_history", "driver_failed_on", "played_out_peak_gain_pct",
    "played_out_peak_date",
)


def repair(ledger: dict) -> list[str]:
    done: list[str] = []
    positions = ledger.get("positions", {})
    trades = ledger.get("trades", [])

    for ticker in TICKERS:
        pos = positions.get(ticker)
        set_driver = next(
            (t for t in trades
             if t.get("action") == "SET_DRIVER" and t.get("ticker") == ticker
             and t.get("date") == RUN_DATE),
            None,
        )
        driver = (set_driver or {}).get("forward_driver") or (pos or {}).get("forward_driver")

        # --- position -------------------------------------------------------
        if pos is not None and pos.get("thesis_played_out"):
            history = pos.get("forward_driver_history") or []
            only_this_run = all(h.get("date") == RUN_DATE for h in history)
            if only_this_run and driver:
                pos["thesis"] = f"[Re-underwritten {RUN_DATE}] {driver}"
                pos["thesis_reunderwritten"] = RUN_DATE
                for key in PLAYED_OUT_FIELDS:
                    pos.pop(key, None)
                done.append(f"{ticker}: played-out fields removed, driver moved "
                            f"into the thesis as the re-underwritten case")

        # --- SET_DRIVER -> SET_THESIS -----------------------------------------
        if set_driver is not None:
            trim = next(
                (t for t in trades
                 if t.get("action") == "TRIM" and t.get("ticker") == ticker
                 and t.get("date") == RUN_DATE),
                None,
            )
            replaced = (trim or {}).get("entry_thesis") or ""
            set_driver.clear()
            set_driver.update({
                "date":            RUN_DATE,
                "action":          "SET_THESIS",
                "ticker":          ticker,
                "thesis":          f"[Re-underwritten {RUN_DATE}] {driver}",
                "replaces_thesis": replaced,
                "note": ("filed as SET_DRIVER on the day; corrected by "
                         "fix_reunderwrite_records.py"),
            })
            done.append(f"{ticker}: SET_DRIVER trade record rewritten as SET_THESIS")

        # --- the forced TRIM ---------------------------------------------------
        for t in trades:
            if (t.get("action") == "TRIM" and t.get("ticker") == ticker
                    and t.get("date") == RUN_DATE
                    and "exit_thesis_original" not in t):
                t["exit_thesis_original"] = t.get("exit_thesis", "")
                t["exit_thesis"] = TRIM_EXIT_THESIS
                t["process_defect"] = True
                done.append(f"{ticker}: forced TRIM annotated as a process defect")

    return done


def main() -> int:
    with open(LEDGER, encoding="utf-8") as f:
        ledger = json.load(f)

    done = repair(ledger)
    if not done:
        print("Nothing to repair — records already correct.")
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
