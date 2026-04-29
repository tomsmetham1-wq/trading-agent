"""
sync_watch.py — Drop-zone watcher for the trading agent project.

Run once in a terminal and leave it. When you click "Download All" in Claude
and save the zip as 'trading' (arriving as trading.zip), this script detects
it, extracts any tracked files, and copies them straight into the project
directory — no VS Code needed.

Also handles individually downloaded files in case you download one at a time.

Usage:
    python sync_watch.py                         # uses defaults below
    python sync_watch.py --project ~/dev/trading # custom project path
    python sync_watch.py --watch ~/Desktop       # watch a different folder

No extra dependencies — uses only the standard library.
"""

import argparse
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Defaults — edit these if you prefer not to use CLI flags
# ---------------------------------------------------------------------------

# Files that will be auto-synced when found in the watch folder or inside a zip
TRACKED_FILES = {
    "trading_agent.py",
    "shadow_portfolio.py",
    "t212_executor.py",
}

# Name you give the zip when Claude asks (without .zip)
ZIP_STEM = "trading"

# How often to check the watch folder (seconds)
POLL_INTERVAL = 2

# How many backup versions to keep per file (0 = no backups)
MAX_BACKUPS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def default_downloads() -> Path:
    return Path.home() / "Downloads"


def default_project() -> Path:
    return Path(__file__).parent.resolve()


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{timestamp()}] {msg}", flush=True)


def backup_existing(dest: Path, max_backups: int) -> None:
    """Save a timestamped backup of dest before overwriting it."""
    if not dest.exists() or max_backups == 0:
        return
    backup_dir = dest.parent / ".sync_backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(dest, backup_dir / f"{dest.name}.{stamp}.bak")
    backups = sorted(backup_dir.glob(f"{dest.name}.*.bak"))
    for old in backups[:-max_backups]:
        old.unlink()


def numbered_variants(watch_dir: Path, stem: str, ext: str) -> list[Path]:
    """Return all files matching stem.ext, stem (1).ext, stem 2.ext, etc."""
    patterns = [
        f"{stem}{ext}",
        f"{stem} (*){ext}",
        f"{stem} [0-9]*{ext}",
    ]
    found = []
    for pat in patterns:
        found.extend(watch_dir.glob(pat))
    return list({p.resolve(): p for p in found}.values())


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

class Watcher:
    def __init__(self, watch_dir: Path, project_dir: Path,
                 tracked: set, zip_stem: str, poll: int, max_backups: int):
        self.watch_dir = watch_dir
        self.project_dir = project_dir
        self.tracked = tracked
        self.zip_stem = zip_stem
        self.poll = poll
        self.max_backups = max_backups
        # canonical filename -> mtime of last synced source
        self._seen_files: dict[str, float] = {}
        # zip path -> mtime of last processed zip
        self._seen_zips: dict[str, float] = {}

    # -- ZIP handling --------------------------------------------------------

    def _zip_candidates(self) -> list[Path]:
        """Find all downloads/trading.zip, trading (1).zip, etc."""
        return numbered_variants(self.watch_dir, self.zip_stem, ".zip")

    def _process_zip(self, zip_path: Path) -> None:
        """Extract tracked files from a zip and copy them to the project."""
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names_in_zip = zf.namelist()
                synced_any = False
                for tracked_name in self.tracked:
                    # File may be at root or inside a subfolder in the zip
                    matches = [n for n in names_in_zip
                               if Path(n).name == tracked_name]
                    if not matches:
                        continue
                    # Prefer root-level match, otherwise take the first
                    src_name = next(
                        (n for n in matches if "/" not in n.rstrip("/")),
                        matches[0]
                    )
                    dest = self.project_dir / tracked_name
                    backup_existing(dest, self.max_backups)
                    with zf.open(src_name) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    log(f"✓ Extracted {tracked_name} from {zip_path.name}  →  {dest}")
                    synced_any = True
                if not synced_any:
                    log(f"  (zip {zip_path.name} contained no tracked files — skipped)")
        except zipfile.BadZipFile:
            log(f"  ! {zip_path.name} is not a valid zip yet — will retry")

    def _check_zips(self) -> None:
        for zip_path in self._zip_candidates():
            key = str(zip_path)
            mtime = zip_path.stat().st_mtime
            if mtime <= self._seen_zips.get(key, 0):
                continue
            self._seen_zips[key] = mtime
            log(f"  Zip detected: {zip_path.name}")
            self._process_zip(zip_path)

    # -- Individual file handling --------------------------------------------

    def _check_individual_files(self) -> None:
        for name in self.tracked:
            stem = Path(name).stem
            ext = Path(name).suffix
            candidates = numbered_variants(self.watch_dir, stem, ext)
            if not candidates:
                continue
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            mtime = newest.stat().st_mtime
            if mtime <= self._seen_files.get(name, 0):
                continue
            dest = self.project_dir / name
            backup_existing(dest, self.max_backups)
            shutil.copy2(newest, dest)
            self._seen_files[name] = mtime
            label = newest.name if newest.name != name else name
            log(f"✓ Synced {label}  →  {dest}")

    # -- Seeding (ignore already-present files on startup) -------------------

    def _seed_seen(self) -> None:
        for zip_path in self._zip_candidates():
            self._seen_zips[str(zip_path)] = zip_path.stat().st_mtime

        for name in self.tracked:
            stem = Path(name).stem
            ext = Path(name).suffix
            for path in numbered_variants(self.watch_dir, stem, ext):
                mtime = path.stat().st_mtime
                if mtime > self._seen_files.get(name, 0):
                    self._seen_files[name] = mtime

    # -- Main loop -----------------------------------------------------------

    def run(self) -> None:
        log(f"Watching : {self.watch_dir}")
        log(f"Project  : {self.project_dir}")
        log(f"Zip name : {self.zip_stem}.zip  (rename to match ZIP_STEM if different)")
        log(f"Tracking : {', '.join(sorted(self.tracked))}")
        log("Waiting for downloads... (Ctrl-C to stop)\n")
        self._seed_seen()

        while True:
            self._check_zips()
            self._check_individual_files()
            time.sleep(self.poll)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-sync Claude-generated project files from Downloads → project dir."
    )
    p.add_argument("--watch", type=Path, default=default_downloads(), metavar="DIR",
                   help=f"Folder to watch (default: {default_downloads()})")
    p.add_argument("--project", type=Path, default=default_project(), metavar="DIR",
                   help="Project directory to sync into (default: script's own directory)")
    p.add_argument("--zip-stem", type=str, default=ZIP_STEM,
                   help=f"Base name of the downloaded zip, without .zip (default: {ZIP_STEM})")
    p.add_argument("--no-backups", action="store_true",
                   help="Skip creating .sync_backups/ before overwriting")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL,
                   help=f"Poll interval in seconds (default: {POLL_INTERVAL})")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    watch_dir = args.watch.expanduser().resolve()
    project_dir = args.project.expanduser().resolve()

    if not watch_dir.is_dir():
        sys.exit(f"ERROR: watch directory does not exist: {watch_dir}")
    if not project_dir.is_dir():
        sys.exit(f"ERROR: project directory does not exist: {project_dir}")

    watcher = Watcher(
        watch_dir=watch_dir,
        project_dir=project_dir,
        tracked=TRACKED_FILES,
        zip_stem=args.zip_stem,
        poll=args.interval,
        max_backups=0 if args.no_backups else MAX_BACKUPS,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()
