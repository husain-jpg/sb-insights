"""
SB Insights launcher — one command to rule them all.

What it does:
1. Checks for new Cova Excel exports in the ./imports folder
2. Imports any it finds into the database
3. Starts the FastAPI dashboard backend
4. Opens your browser to the dashboard

Usage:
    python run.py

First-time setup:
    python run.py --setup
"""

import argparse
import os
import sys
import subprocess
import webbrowser
from pathlib import Path


def print_banner():
    print()
    print("=" * 60)
    print("  SB INSIGHTS — Cannabis Retail Dashboard")
    print("=" * 60)
    print()


def ensure_folders():
    """Create the folders we need if they don't exist."""
    Path("imports").mkdir(exist_ok=True)
    Path("imports/processed").mkdir(exist_ok=True)


def import_new_files():
    """Look for xlsx files in ./imports and load them into the DB."""
    imports = Path("imports")
    # Include .csv: run_import processes csv too, so they must also be moved to
    # processed/ afterward — otherwise they pile up in imports/ and get
    # re-imported on every startup (slow).
    files = (list(imports.glob("*.xlsx")) + list(imports.glob("*.xls"))
             + list(imports.glob("*.csv")))
    if not files:
        print("→ No new files in ./imports (that's okay if you've already imported)")
        return

    print(f"→ Found {len(files)} file(s) to import. Processing...")
    print()
    result = subprocess.run(
        [sys.executable, "jobs/import_cova_exports.py", str(imports)],
        env={**os.environ, "PYTHONPATH": "."},
    )
    if result.returncode != 0:
        print("✗ Import failed. Fix errors above before continuing.")
        sys.exit(1)

    # Move processed files out of the way so we don't re-import every time
    processed = imports / "processed"
    for f in files:
        dest = processed / f.name
        # If already exists, add a suffix
        i = 1
        while dest.exists():
            dest = processed / f"{f.stem}_{i}{f.suffix}"
            i += 1
        f.rename(dest)
    print(f"→ Moved imported files to ./imports/processed/")


def start_dashboard():
    """Launch uvicorn serving the API, and the browser pointed at the dashboard."""
    print()
    print("→ Starting dashboard server...")
    print("→ Will open in your browser when ready.")
    print()
    print("  To stop the server: close this window or press Ctrl+C")
    print()

    # Open browser after a short delay (so the server has time to start)
    import threading, time
    def open_browser_later():
        time.sleep(2.5)
        webbrowser.open("http://localhost:8000/")
    threading.Thread(target=open_browser_later, daemon=True).start()

    # Run uvicorn inline so the user sees the logs
    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "api.main:app",
             "--host", "127.0.0.1", "--port", "8000"],
        )
    except KeyboardInterrupt:
        print("\n→ Server stopped. Goodbye.")


def check_setup():
    """Friendly check that everything's installed."""
    problems = []

    try:
        import pandas
        import openpyxl
    except ImportError as e:
        problems.append(f"Missing Python package: {e.name}. Run: pip install -r requirements.txt")

    try:
        import fastapi
        import uvicorn
    except ImportError as e:
        problems.append(f"Missing Python package: {e.name}. Run: pip install -r requirements.txt")

    # python-multipart is a separate package from FastAPI, required for file uploads
    try:
        import multipart  # noqa: F401
    except ImportError:
        problems.append("Missing Python package: python-multipart. Run: pip install python-multipart")

    if problems:
        print()
        print("SETUP INCOMPLETE:")
        for p in problems:
            print(f"  ✗ {p}")
        print()
        print("Once fixed, run `python run.py` again.")
        sys.exit(1)

    print("✓ All required packages installed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="Just check setup and exit.")
    parser.add_argument("--no-import", action="store_true", help="Skip the import step.")
    args = parser.parse_args()

    print_banner()
    ensure_folders()
    check_setup()

    if args.setup:
        print("\n✓ Setup OK. To launch the dashboard, run:  python run.py")
        return

    if not args.no_import:
        import_new_files()

    start_dashboard()


if __name__ == "__main__":
    main()
