"""Capture README screenshots with headless Chrome. Start the API first: make run.

Usage: uv run python scripts/screenshot.py --port 8874
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]
DOCS = Path(__file__).resolve().parents[1] / "docs"


def chrome() -> str:
    for c in CHROME_PATHS:
        if Path(c).exists() or shutil.which(c):
            return c
    raise SystemExit("Chrome not found")


def shoot(url: str, out: Path, width: int, height: int, dark: bool = False) -> None:
    args = [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={width},{height}",
            "--virtual-time-budget=6000", f"--screenshot={out}"]
    if dark:
        args += ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"]
    else:
        args += ["--blink-settings=preferredColorScheme=1"]
    subprocess.run([*args, url], check=True, capture_output=True, timeout=60)
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8874)
    ap.add_argument("--customer", default="c196", help="customer id for the customer page")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    with urllib.request.urlopen(f"{base}/healthz") as r:
        json.load(r)
    DOCS.mkdir(exist_ok=True)
    shoot(f"{base}/?customer={a.customer}#customer", DOCS / "screenshot.png", 1300, 1600)
    shoot(f"{base}/#availability", DOCS / "availability-dark.png", 1300, 1500, dark=True)
    shoot(f"{base}/#finance", DOCS / "finance.png", 1300, 1500)


if __name__ == "__main__":
    main()
