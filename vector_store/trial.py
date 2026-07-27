"""500-document trial: extract, chunk, and report what the cascade actually did.

Runs before any GPU is provisioned, because the failure it is looking for is
invisible in the output. If SPC template detection breaks, the collection still
fills with plausible chunks and retrieval still returns something - only the
section filters go quiet, months later.

Reads ONLY the eight live categories. Old_DataLake and endpoint-schema.md are
excluded by explicit guard, not by relying on the prefix list.

    python trial.py --n 500 --out /tmp/trial
"""
from __future__ import annotations

import argparse
import collections
import io
import os
import random
import sys
import traceback

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config          # noqa: E402
from chunk import chunk_document, _ntok   # noqa: E402

CATEGORIES = [
    "Clinical_Trials_Pipeline_Intelligence",
    "Drug_Substance_Reference",
    "Literature_Evidence",
    "MENA_GCC_Regulatory_Market",
    "Ontologies_Standards",
    "Regulatory_Approvals",
    "Safety_Pharmacovigilance",
    "Targets_Genomics_Biomarkers",
]
# Never read these, whatever else changes.
FORBIDDEN = ("Old_DataLake/", "endpoint-schema.md")

DOC_SUFFIXES = (".pdf", ".doc", ".docx", ".ppt", ".pptx")

# Deliberately not proportional. PMDA is 0.6% of the corpus but carries the only
# Japanese, which is the case the tokenizer fix was written for and the one still
# unvalidated. Sampling proportionally would draw ~3 of them.
QUOTA = [
    ("Regulatory_Approvals/products.mhra.gov.uk/mhra_data/pdfs/spc/", 140, "mhra-spc"),
    ("Regulatory_Approvals/products.mhra.gov.uk/mhra_data/pdfs/pil/", 130, "mhra-pil"),
    ("Regulatory_Approvals/products.mhra.gov.uk/mhra_data/pdfs/par/", 70,  "mhra-par"),
    ("Regulatory_Approvals/ema.europa.eu/",                            80,  "ema"),
    ("Regulatory_Approvals/pmda.go.jp/",                               60,  "pmda"),
    ("MENA_GCC_Regulatory_Market/",                                    19,  "mena"),
    ("Ontologies_Standards/",                                           1,  "ontologies"),
]


def safe(key: str) -> bool:
    if any(f in key for f in FORBIDDEN):
        return False
    return key.split("/")[0] in CATEGORIES


def list_docs(s3, prefix, cap=3000):
    out, token = [], None
    while len(out) < cap:
        kw = {"Bucket": config.S3_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            k = o["Key"]
            if k.lower().endswith(DOC_SUFFIXES) and safe(k):
                out.append(k)
        if not r.get("IsTruncated"):
            break
        token = r["NextContinuationToken"]
    return out


def doc_type_of(key: str):
    if "/pdfs/" in key:
        return key.split("/pdfs/")[1].split("/")[0].lower()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()

    import fitz
    s3 = boto3.client("s3")
    random.seed(a.seed)

    picked = []
    for prefix, quota, label in QUOTA:
        keys = list_docs(s3, prefix)
        if not keys:
            print(f"  {label:12} no documents under {prefix}")
            continue
        take = min(quota, len(keys))
        picked += [(k, label) for k in random.sample(keys, take)]
        print(f"  {label:12} {take:>4} of {len(keys):,} available")
    random.shuffle(picked)
    picked = picked[:a.n]
    print(f"\nsampled {len(picked)} documents\n")

    # A last guard: prove nothing forbidden made it into the sample.
    bad = [k for k, _ in picked if not safe(k)]
    assert not bad, f"forbidden keys in sample: {bad[:3]}"

    paths = collections.Counter()
    sections = collections.Counter()
    langs = collections.Counter()
    by_label = collections.defaultdict(collections.Counter)
    toks, nchunks, failed, empty = [], 0, [], 0
    pages_total = 0

    for i, (key, label) in enumerate(picked, 1):
        try:
            raw = s3.get_object(Bucket=config.S3_BUCKET, Key=key)["Body"].read()
            with fitz.open(stream=raw, filetype="pdf") as d:
                blocks = [(p_i, p.get_text("text")) for p_i, p in enumerate(d, 1)]
                pages_total += d.page_count
            cs = chunk_document(blocks, label, key.rsplit("/", 1)[-1][:60], key,
                                doc_type=doc_type_of(key))
            if not cs:
                empty += 1
                continue
            nchunks += len(cs)
            for c in cs:
                paths[c.chunk_path] += 1
                sections[c.section or "(none)"] += 1
                langs[c.language] += 1
                by_label[label][c.chunk_path] += 1
                toks.append(_ntok(c.text))
        except Exception as e:  # noqa: BLE001
            failed.append((key, f"{type(e).__name__}: {e}"))
        if i % 50 == 0:
            print(f"  {i}/{len(picked)} ... {nchunks:,} chunks", flush=True)

    print("\n" + "=" * 74)
    print(f"documents      : {len(picked)}   failed {len(failed)}   no-text {empty}")
    print(f"pages          : {pages_total:,}")
    print(f"chunks         : {nchunks:,}   ({nchunks/max(len(picked)-len(failed),1):.1f} per doc)")
    if toks:
        toks.sort()
        print(f"tokens/chunk   : avg {sum(toks)/len(toks):.0f}  p50 {toks[len(toks)//2]}  "
              f"p95 {toks[int(len(toks)*0.95)]}  max {toks[-1]}")
        print(f"  under 60 tok : {sum(1 for t in toks if t < 60):,} "
              f"({sum(1 for t in toks if t < 60)/len(toks)*100:.1f}%)")
        print(f"  at budget    : {sum(1 for t in toks if t >= config.CHUNK_TOKENS):,} "
              f"({sum(1 for t in toks if t >= config.CHUNK_TOKENS)/len(toks)*100:.1f}%)")

    print(f"\nchunk_path     : {dict(paths)}")
    print(f"languages      : {dict(langs)}")
    print("\nper source:")
    for label, c in sorted(by_label.items()):
        tot = sum(c.values())
        share = ", ".join(f"{k} {v/tot*100:.0f}%" for k, v in c.most_common())
        print(f"  {label:12} {tot:>7,} chunks   {share}")

    print("\ntop sections:")
    for s, n in sections.most_common(12):
        print(f"  {s:34} {n:>7,}")

    if failed:
        print(f"\nfailures ({len(failed)}):")
        for k, e in failed[:8]:
            print(f"  {k.rsplit('/',1)[-1][:52]:54} {e[:70]}")

    # The checks that matter, stated as pass/fail rather than left to the reader.
    print("\n" + "-" * 74)
    spc = by_label.get("mhra-spc", {})
    spc_tot = sum(spc.values()) or 1
    spc_ok = spc.get("spc", 0) / spc_tot
    print(f"MHRA SPC on the template path : {spc_ok*100:.0f}%  "
          f"{'OK' if spc_ok > 0.9 else 'INVESTIGATE - detection is failing'}")
    pil = by_label.get("mhra-pil", {})
    pil_tot = sum(pil.values()) or 1
    pil_ok = pil.get("pil", 0) / pil_tot
    print(f"MHRA PIL on the leaflet path  : {pil_ok*100:.0f}%  "
          f"{'OK' if pil_ok > 0.9 else 'INVESTIGATE'}")
    ja = langs.get("ja", 0)
    print(f"Japanese chunks detected      : {ja:,}  "
          f"{'OK' if ja else 'none seen - the CJK path is still unvalidated'}")


if __name__ == "__main__":
    main()
