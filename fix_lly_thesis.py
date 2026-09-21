"""
One-off correction of the LLY entry thesis recorded on 2026-09-21.

The buy case named "Qulipta Phase 3 win adding a second pipeline leg".
Qulipta (atogepant) is AbbVie's migraine drug — the same report's ABBV
section says so — and it is not part of Lilly's pipeline. Left in place, the
weekly thesis review would keep "confirming" a leg the company does not
have. The GLP-1 case (Mounjaro/Zepbound, cardiovascular and NASH
indications, valuation range, diversification) is the actual thesis and is
kept verbatim; only the wrong clause is removed.

Applied as a SET_THESIS through sp._apply_set_thesis() so the position and
the trade log carry exactly what a run would have written: the thesis is
stamped "[Re-underwritten 2026-09-21]" (the entry date, so provenance still
scores it from entry) and the text it replaced is kept on the trade record.

Idempotent. Run with --apply to write; backs the ledger up first.
"""

import shutil
import sys
from datetime import datetime

import shadow_portfolio as sp

LEDGER   = "shadow_portfolio.json"
RUN_DATE = "2026-09-21"
TICKER   = "LLY"

WRONG_CLAUSE = "Qulipta Phase 3 win adding a second pipeline leg, "

CORRECTED = (
    "Mounjaro/Zepbound GLP-1 franchise with expanding cardiovascular and NASH "
    "indications, entering the 28-30x target valuation range after a 7% "
    "pullback from watchlist entry — zero AI-theme correlation diversifies the "
    "book. (Corrected 2026-09-21: the entry case also cited a Qulipta Phase 3 "
    "win as a second pipeline leg; Qulipta is AbbVie's drug, not Lilly's, and "
    "that leg is withdrawn.)"
)


def main(apply: bool) -> int:
    ledger = sp.load_ledger()
    pos = ledger.get("positions", {}).get(TICKER)
    if pos is None:
        print(f"{TICKER}: no position held - nothing to do")
        return 0
    current = pos.get("thesis") or ""
    if WRONG_CLAUSE not in current:
        print(f"{TICKER}: thesis does not contain the Qulipta clause - already "
              f"corrected or never wrong")
        print(f"  current: {current}")
        return 0

    print(f"{TICKER} thesis before:\n  {current}\n")
    print(f"{TICKER} thesis after:\n  [Re-underwritten {RUN_DATE}] {CORRECTED}\n")

    if not apply:
        print("Dry run. Re-run with --apply to write.")
        return 0

    backup = f"{LEDGER}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copyfile(LEDGER, backup)
    print(f"Backed up ledger to {backup}")

    event = sp._apply_set_thesis(
        ledger, {"action": "SET_THESIS", "ticker": TICKER, "thesis": CORRECTED},
        TICKER, RUN_DATE)
    print(event)
    sp.save_ledger(ledger)
    print("Ledger saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
