"""Watch a local inbox folder and rebuild the CFO report when a new bank CSV arrives.

Daily workflow: start this watcher once, then drop each day's bank CSV into the
inbox/ folder. The watcher notices the file, runs the same five-agent ADK
pipeline as main.py (security gate included), and refreshes the dashboard.
Recurring items approved earlier via --review are reused; new uncertain items
are conservatively included and disclosed in the report - nothing is ever
auto-approved without a human.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from main import run_adk_pipeline


def scan_ready(folder: Path, snapshots: dict, processed: dict) -> list[Path]:
    """Return CSV files that are new or changed and stable since the last scan.

    A file becomes ready only when its (mtime, size) signature is identical on
    two consecutive scans, so files still being copied are never processed.
    ``snapshots`` is updated in place; the caller marks handled files in
    ``processed`` to prevent re-runs.
    """
    ready: list[Path] = []
    for path in sorted(folder.glob("*.csv")):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = (stat.st_mtime_ns, stat.st_size)
        if snapshots.get(path.name) == signature and processed.get(path.name) != signature:
            ready.append(path)
        snapshots[path.name] = signature
    return ready


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Rebuild the CFO report whenever a new bank CSV lands in the inbox folder.")
    parser.add_argument("--inbox", default="inbox", help="Watched folder inside this workspace")
    parser.add_argument("--output", default="output/cfo_control_tower.html", help="HTML report path inside this workspace")
    parser.add_argument("--poll", type=float, default=2.0, help="Seconds between folder scans")
    parser.add_argument("--iterations", type=int, default=0, help="Stop after N scans (0 = run until Ctrl+C); mainly for tests")
    parser.add_argument("--no-open", action="store_true", help="Do not open the refreshed report in the browser")
    args = parser.parse_args()

    inbox = Path(args.inbox)
    inbox.mkdir(parents=True, exist_ok=True)

    snapshots: dict = {}
    processed: dict = {}
    for path in inbox.glob("*.csv"):
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        snapshots[path.name] = signature
        processed[path.name] = signature

    print(f"Watching {inbox.resolve()} for new bank CSV files. Press Ctrl+C to stop.")
    if processed:
        print(f"{len(processed)} existing file(s) ignored; drop a new CSV to trigger a report.")

    scans = 0
    try:
        while True:
            scans += 1
            for path in scan_ready(inbox, snapshots, processed):
                signature = snapshots[path.name]
                print(f"\nNew bank data detected: {path.name} - running the five-agent pipeline...")
                try:
                    result = asyncio.run(run_adk_pipeline(str(path), args.output))
                except (ValueError, FileNotFoundError, RuntimeError) as exc:
                    print(f"Error: {exc}")
                else:
                    print(f"Report updated: {result['report']} (as of {result['as_of_date']})")
                    if not args.no_open:
                        webbrowser.open(Path(result["report"]).as_uri())
                processed[path.name] = signature
            if args.iterations and scans >= args.iterations:
                return 0
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
