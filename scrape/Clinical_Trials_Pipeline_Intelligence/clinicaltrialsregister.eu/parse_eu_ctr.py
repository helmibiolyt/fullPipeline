#!/usr/bin/env python3
"""Parse EU CTR 'full details' text (page_*.txt) into a single CSV.

The EU CTR download endpoint serves plain text, not CSV. Each trial record
starts with a line 'Summary'; fields are 'Label: value' (header) or
'Code Label: value' (EudraCT body, e.g. 'A.3 Full title of the trial: ...').
One CSV row per Summary block; the label (incl. code) becomes the column.

Two passes (re-reads files) to stay memory-safe on large downloads.
"""
import csv
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def iter_records(page_files):
    """Yield one list-of-lines per Summary block across all page files."""
    cur = None
    for pf in page_files:
        with open(pf, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip() == "Summary":
                    if cur is not None:
                        yield cur
                    cur = []
                elif cur is not None:
                    cur.append(line.rstrip("\n"))
    if cur is not None:
        yield cur


def parse_record(lines) -> dict:
    rec, last = {}, None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k:
                rec[k] = (rec[k] + " | " + v) if (k in rec and v) else rec.get(k) or v
                last = k
        elif last:                       # continuation of the previous field
            rec[last] = (rec[last] + " " + s).strip()
    return rec


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "eu_ctr_trials"
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    pages = sorted(out_dir.glob("page_*.txt"))
    if not pages:
        sys.exit(f"no page_*.txt found in {out_dir}")

    cols = {}                            # pass 1: union of column keys (order-preserving)
    for rec_lines in iter_records(pages):
        for k in parse_record(rec_lines):
            cols.setdefault(k, None)

    out_csv = out_dir / "eu_ctr_all_trials.csv"
    n = 0                                # pass 2: write rows
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cols), extrasaction="ignore")
        w.writeheader()
        for rec_lines in iter_records(pages):
            w.writerow(parse_record(rec_lines))
            n += 1
    print(f"parsed {n} records -> {out_csv} ({len(cols)} columns)")


if __name__ == "__main__":
    main()
