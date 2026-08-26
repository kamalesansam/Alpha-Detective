"""
combine_data.py — Data Preparation for Alpha-Detective
=======================================================

Loops through an earnings-call transcript dataset folder, reads every text
file, extracts company / ticker / quarter information, and combines everything
into a single pandas DataFrame saved as ``earnings_transcripts.csv`` with the
columns: ``Ticker``, ``Company_Name``, ``Quarter``, ``Text``.

The script AUTO-DETECTS two possible dataset layouts:

1. Nested layout (the actual NLP_Dataset attached to this project)::

       NLP_Dataset/
         Accenture/
           2018_Q1_acn.txt
           2018_Q2_acn.txt
           ...
         Adobe/
           ...

   -> Company_Name is taken from the *folder* name,
      Ticker is the filename part after the last underscore (e.g. ``acn``),
      Quarter is parsed from the filename middle part (e.g. ``2018-Q1``).

2. Flat layout (one file per company, as in the original task brief)::

       cleaned_ECTs_dataset/
         Apple.txt
         Microsoft.txt
         ...

   -> Company_Name is the filename without extension,
      Ticker falls back to a short slug of the company name,
      Quarter is set to "N/A".

Usage::

    python combine_data.py                          # default: ./NLP_Dataset
    python combine_data.py --data-dir cleaned_ECTs_dataset --output out.csv
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_DATA_DIR = "NLP_Dataset"
DEFAULT_OUTPUT = "earnings_transcripts.csv"

# Matches filename patterns such as ``2018_Q1_acn.txt`` or ``2020-Q2_aapl.txt``
# Group 1 -> year, Group 2 -> quarter, Group 3 -> ticker.
FILENAME_PATTERN = re.compile(
    r"^(?P<year>\d{4})[_\-](?P<quarter>[Qq][1-4])[_\-](?P<ticker>[A-Za-z0-9+.]*?)"
    r"\.(?:txt|csv)$"
)

# The ticker part is expected to be the token after the last underscore, which
# also works for filenames with a suffix such as ``acn_final.txt``.
TRAILING_TICKER_PATTERN = re.compile(r"[_\-]([A-Za-z0-9+.]*?)(?:\.(?:txt|csv))?$")


# --------------------------------------------------------------------------- #
# Reading helpers
# --------------------------------------------------------------------------- #

def read_text_robust(path: Path) -> str:
    """Read a transcript file with encoding fallbacks.

    The transcripts are mostly UTF-8 but occasionally contain Windows-1252
    smart punctuation (e.g. 0x92 right single quotation mark), which makes a
    strict UTF-8 decode fail.  We try UTF-8 first and fall back to cp1252.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # cp1252 is a superset of latin-1 for the byte range used by smart
        # quotes; it never raises, so this branch always succeeds.
        return raw.decode("cp1252")


# --------------------------------------------------------------------------- #
# Layout detection / per-file metadata extraction
# --------------------------------------------------------------------------- #

def is_flat_layout(data_dir: Path) -> bool:
    """Return True when transcripts live directly in ``data_dir`` (no company
    subfolders), i.e. the layout described in the original task brief."""
    for child in data_dir.iterdir():
        if child.is_file() and child.suffix.lower() in {".txt", ".csv"}:
            return True
    return False


def parse_quarter(filename: str) -> str:
    """Extract a readable quarter string (e.g. ``2018-Q1``) from a filename.

    Returns ``"N/A"`` when the filename carries no quarter information.
    """
    m = re.match(r"^(\d{4})[_\-]([Qq][1-4])(?:_|\-|\.)", filename)
    if m:
        return f"{m.group(1)}-{m.group(2).upper()}"
    return "N/A"


def ticker_from_filename(filename: str) -> str:
    """Best-effort ticker extraction from a filename.

    Takes the token after the last underscore/hyphen (``2018_Q1_acn.txt`` ->
    ``acn``).  Falls back to the lowercase filename stem otherwise.
    """
    stem = Path(filename).stem
    m = TRAILING_TICKER_PATTERN.search(stem)
    candidate = m.group(1) if m else stem
    # A real ticker should look like a short alphanumeric token (``+`` allowed
    # for names like ``volv+b``).
    if re.fullmatch(r"[A-Za-z0-9+.]{1,10}", candidate):
        return candidate.upper()
    return stem.upper()


def slugify(name: str) -> str:
    """Ticker fallback for the flat layout: company name -> short uppercase slug."""
    slug = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    return slug[:5] if slug else "UNKNOWN"


def collect_rows(data_dir: Path) -> list[dict]:
    """Walk the dataset folder and return one dict per transcript file."""
    files: list[Path] = []
    if is_flat_layout(data_dir):
        # Layout 2: every file in the root IS one transcript/company file.
        files = [p for p in data_dir.iterdir() if p.is_file()]
    else:
        # Layout 1: one subfolder per company, transcripts inside.
        for folder in sorted(data_dir.iterdir()):
            if folder.is_dir():
                files.extend(sorted(folder.glob("*.txt")))
                files.extend(sorted(folder.glob("*.csv")))

    if not files:
        raise FileNotFoundError(
            f"No .txt/.csv transcript files found under '{data_dir}'. "
            "Check the --data-dir argument."
        )

    rows: list[dict] = []
    for path in files:
        text = read_text_robust(path)
        fname = path.name

        if is_flat_layout(data_dir):
            company = Path(fname).stem
            ticker = ticker_from_filename(fname) or slugify(company)
            quarter = parse_quarter(fname) or "N/A"
            if quarter == "N/A":
                # Flat layout per original brief: quarter unknown.
                ticker = slugify(company) if company.lower() == ticker.lower() else ticker
        else:
            company = path.parent.name  # folder name == company name
            m = FILENAME_PATTERN.match(fname)
            if m:
                ticker = m.group("ticker").upper()
                quarter = f"{m.group('year')}-{m.group('quarter').upper()}"
            else:
                ticker = ticker_from_filename(fname)
                quarter = parse_quarter(fname)

        rows.append(
            {
                "Ticker": ticker,
                "Company_Name": company,
                "Quarter": quarter,
                "Text": text,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help=f"Path to the transcript dataset folder (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"[ERROR] Data folder not found: {data_dir}", file=sys.stderr)
        return 1

    print(f"[1/3] Scanning {data_dir} ...")
    rows = collect_rows(data_dir)

    df = pd.DataFrame(rows, columns=["Ticker", "Company_Name", "Quarter", "Text"])
    df = df.drop_duplicates(subset=["Ticker", "Company_Name", "Quarter", "Text"])
    df.to_csv(args.output, index=False, encoding="utf-8")

    print(f"[2/3] Combined {len(df):,} transcripts "
          f"({df['Ticker'].nunique()} unique tickers, "
          f"{df['Company_Name'].nunique()} companies).")
    print(f"[3/3] Saved -> {args.output}")
    print(df.groupby("Company_Name").size().sort_values(ascending=False).head(10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())