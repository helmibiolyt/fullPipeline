#!/usr/bin/env python3
"""Run the full pipeline for ONE source outside Airflow — for testing.

    # dry: scrape + collect + validate locally, no S3
    PYTHONPATH=plugins python run_source.py uniprot_org --no-s3

    # full: also upload -> verify -> commit -> prune to S3
    PYTHONPATH=plugins python run_source.py uniprot_org

Set SCRAPE_ROOT / RUN_ROOT / S3_BUCKET / AWS_PROFILE in the environment first
(see docker-compose.yaml for the expected values).
"""
import argparse
import logging
import sys

from scrape_pipeline.registry import load_sources
from scrape_pipeline import linked_docs, runner, validation, s3_io

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug_suffix", help="unique tail of a source slug, e.g. uniprot_org")
    ap.add_argument("--run-id", default="local__manual")
    ap.add_argument("--no-s3", action="store_true", help="stop after local validation")
    ap.add_argument("--skip-scrape", action="store_true",
                    help="reuse existing scraper output (skip running the scraper)")
    a = ap.parse_args()

    matches = [s for s in load_sources() if s.slug.endswith(a.slug_suffix)]
    if len(matches) != 1:
        sys.exit(f"expected 1 source matching '{a.slug_suffix}', got {len(matches)}")
    src = matches[0]

    if not a.skip_scrape:
        runner.run_scraper(src, a.run_id)          # runs the scraper (no collect)
    runner.collect(src, a.run_id)                  # snapshot CSV + raw docs (xlsx->csv)
    linked_docs.fetch_linked(src, a.run_id)        # fetch pdfs the CSVs only link to
    validation.validate_local(src, a.run_id)

    if a.no_s3:
        print("OK (local only). Skipping S3.")
        return

    s3_io.upload_run(src, a.run_id)
    s3_io.verify_run(src, a.run_id)
    s3_io.commit(src, a.run_id)
    s3_io.prune_runs(src)
    runner.cleanup_local(src, a.run_id)   # mirror the DAG's success path
    print("OK (committed to S3).")


if __name__ == "__main__":
    main()
