#!/usr/bin/env python3
"""
bioRxiv Preprint Metadata Downloader
Author: Antigravity AI Coding Assistant
Description: Extracts preprint metadata from the Cold Spring Harbor Laboratory (CSHL) bioRxiv API,
             including DOIs, titles, abstracts, authors, and corresponding author institutions.
             Includes cursor-based resume capabilities, de-duplication, and rate limiting.

             The CSHL /details/ endpoint is cursor-paginated in steps of 100 and the total
             record count is known from the first response (messages[0].total). Once the total
             is known, the remaining page offsets are independent and are fetched in parallel via
             a bounded ThreadPoolExecutor. Every outbound request is routed through a single
             module-level, thread-safe rate limiter so the COMBINED request rate across all
             worker threads stays under a conservative cap (~5 req/s), with back-off on 429/503.
"""

import os
import sys
import json
import time
import csv
import argparse
import logging
import datetime
import threading
import concurrent.futures
from pathlib import Path
import requests
from dotenv import load_dotenv

# Base directory for relative paths
BASE_DIR = Path(__file__).resolve().parent

# Load variables from .env
load_dotenv()

# Standard headers to play nice with CSHL servers
HEADERS = {
    "User-Agent": "BiolytInternPreprintDownloader/1.0 (Python; requests; Antigravity)",
}

# The CSHL /details/ endpoint returns records in fixed pages of 100.
PAGE_SIZE = 100

# Conservative COMBINED cap across ALL worker threads (requests / second).
# api.biorxiv.org publishes no hard limit; keep it gentle for a free public API.
MAX_REQUESTS_PER_SECOND = 5.0

logger = logging.getLogger("biorxiv_downloader")


class RateLimiter:
    """
    Thread-safe minimum-interval gate shared by every worker thread.

    Each acquire() reserves the next evenly spaced time slot (min_interval apart)
    under a lock, releases the lock, then sleeps until that slot outside the lock.
    This bounds the COMBINED dispatch rate across all threads to ``rate_per_second``
    without serializing the actual network waits.
    """

    def __init__(self, rate_per_second: float):
        self.min_interval = (1.0 / rate_per_second) if rate_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self.min_interval
            wait = slot - now
        if wait > 0:
            time.sleep(wait)


# Single module-level limiter shared by every thread.
RATE_LIMITER = RateLimiter(MAX_REQUESTS_PER_SECOND)


def setup_logging(verbose: bool, log_file: Path):
    """Configures the logging output level and destination."""
    log_level = logging.DEBUG if verbose else logging.INFO

    # Ensure parent dir exists
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8")
        ]
    )


def fetch_api_page(server: str, start_date: str, end_date: str, cursor: int, retries: int = 5, delay: float = 1.0) -> dict:
    """
    Downloads a single page of metadata from CSHL bioRxiv API with retries and exponential backoff.
    Every request is gated through the shared RATE_LIMITER so the combined rate across all
    threads stays under the cap. HTTP 429/503 trigger a polite back-off (honoring Retry-After).
    Format: https://api.biorxiv.org/details/[server]/[interval]/[cursor]/json
    """
    interval = f"{start_date}/{end_date}"
    url = f"https://api.biorxiv.org/details/{server}/{interval}/{cursor}/json"

    backoff = 2.0
    for attempt in range(retries):
        try:
            # Gate every outbound request through the shared limiter.
            RATE_LIMITER.acquire()
            logger.debug(f"Fetching: {url} (Attempt {attempt+1}/{retries})")
            r = requests.get(url, headers=HEADERS, timeout=30)

            # Explicit back-off on rate-limit / temporary-unavailable responses.
            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After")
                if retry_after and str(retry_after).strip().isdigit():
                    sleep_time = float(retry_after)
                else:
                    sleep_time = backoff ** attempt + delay
                logger.warning(
                    f"Received HTTP {r.status_code} (rate limited / unavailable) for {url}. "
                    f"Backing off {sleep_time:.2f}s (attempt {attempt+1}/{retries})."
                )
                time.sleep(sleep_time)
                continue

            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as he:
            if he.response is not None and he.response.status_code == 404:
                # Sometimes API returns 404 if no records exist in the range
                logger.warning(f"API returned 404 for URL: {url}. Treating as empty response.")
                return {"messages": [{"status": "no posts found"}]}
            logger.warning(f"HTTP error on attempt {attempt+1}: {he}")
        except Exception as e:
            logger.warning(f"Connection error on attempt {attempt+1}: {e}")

        if attempt < retries - 1:
            sleep_time = backoff ** attempt + delay
            logger.info(f"Retrying in {sleep_time:.2f} seconds...")
            time.sleep(sleep_time)

    raise Exception(f"Failed to fetch data from API after {retries} attempts.")


def parse_date(date_str: str) -> str:
    """Validates and parses YYYY-MM-DD date string."""
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: '{date_str}'. Must be YYYY-MM-DD.")


def _row_from_item(item: dict, server: str) -> dict:
    """Maps an API record onto the fixed CSV schema."""
    return {
        "doi": item.get("doi", "").strip(),
        "title": item.get("title", ""),
        "authors": item.get("authors", ""),
        "author_corresponding": item.get("author_corresponding", ""),
        "author_corresponding_institution": item.get("author_corresponding_institution", ""),
        "date": item.get("date", ""),
        "version": item.get("version", ""),
        "type": item.get("type", ""),
        "license": item.get("license", ""),
        "category": item.get("category", ""),
        "jatsxml": item.get("jatsxml", ""),
        "abstract": item.get("abstract", ""),
        "funder": item.get("funder", ""),
        "published": item.get("published", ""),
        "server": item.get("server", server),
    }


def run_downloader(server: str, output_dir: Path, start_date: str, end_date: str,
                   limit: int, delay: float, fresh: bool, threads: int):
    """Executes the crawl pipeline with bounded, rate-limited parallel page fetches."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Files
    metadata_csv_file = output_dir / f"{server}_metadata.csv"
    progress_file = output_dir / f"{server}_progress.json"

    # Load existing progress
    cursor = 0
    processed_count = 0
    pages_count = 0

    if not fresh and progress_file.exists():
        try:
            with open(progress_file, "r", encoding="utf-8") as pf:
                progress = json.load(pf)
            # Verify range to prevent resuming under different criteria
            if (progress.get("start_date") == start_date and
                    progress.get("end_date") == end_date and
                    progress.get("server") == server):
                cursor = progress.get("next_cursor", 0)
                processed_count = progress.get("total_records_processed", 0)
                pages_count = progress.get("total_pages_processed", 0)
                logger.info(f"Resuming progress: cursor={cursor}, processed={processed_count}, pages={pages_count}")
            else:
                logger.warning("Crawl parameters changed. Starting fresh crawl.")
        except Exception as e:
            logger.warning(f"Could not load progress file: {e}. Starting fresh.")

    # Snap the resume cursor onto the API page grid (offsets are multiples of PAGE_SIZE).
    cursor = (cursor // PAGE_SIZE) * PAGE_SIZE

    # Load completed records for de-duplication
    completed_dois = set()
    if not fresh and metadata_csv_file.exists():
        try:
            with open(metadata_csv_file, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header:
                    # 'doi' is the first column in the list
                    for row in reader:
                        if row:
                            completed_dois.add(row[0])
            logger.info(f"Loaded {len(completed_dois):,} existing DOIs for de-duplication.")
        except Exception as e:
            logger.warning(f"Could not read existing metadata CSV: {e}")

    # CSV headers
    csv_fields = [
        "doi", "title", "authors", "author_corresponding", "author_corresponding_institution",
        "date", "version", "type", "license", "category", "jatsxml", "abstract",
        "funder", "published", "server"
    ]

    meta_csv_exists = metadata_csv_file.exists() and not fresh

    logger.info(
        f"Starting {server} crawl. Interval: {start_date} to {end_date}. "
        f"Limit: {limit or 'None'}. Threads: {threads}. "
        f"Combined rate cap: {MAX_REQUESTS_PER_SECOND} req/s."
    )

    session_start = time.monotonic()

    # ------------------------------------------------------------------
    # Step 1: fetch the first page (at the resume cursor) to learn the total.
    # ------------------------------------------------------------------
    try:
        first_data = fetch_api_page(server, start_date, end_date, cursor, delay=delay)
    except Exception as e:
        logger.error(f"Error fetching first page at cursor {cursor}: {e}")
        # Ensure a header-only CSV exists for brand-new runs.
        if not meta_csv_exists:
            with open(metadata_csv_file, "w", newline="", encoding="utf-8") as f_csv:
                csv.DictWriter(f_csv, fieldnames=csv_fields).writeheader()
        # The first page failing means we fetched nothing at all — report it as a
        # failure rather than publishing the header-only CSV as a good result.
        return 1

    messages = first_data.get("messages", [])
    status = "unknown"
    total_records = None
    if messages:
        status = messages[0].get("status", "unknown")
        total_records_str = messages[0].get("total")
        if total_records_str and total_records_str != "NA":
            try:
                total_records = int(total_records_str)
            except (ValueError, TypeError):
                pass

    first_collection = first_data.get("collection", []) or []
    if status == "no posts found" or not first_collection:
        logger.info("No posts found in this range. Crawl complete.")
        if not meta_csv_exists:
            with open(metadata_csv_file, "w", newline="", encoding="utf-8") as f_csv:
                csv.DictWriter(f_csv, fieldnames=csv_fields).writeheader()
        return 0  # genuinely empty range, not a failure

    # Map of offset -> collection list. Page 0 (resume cursor) is already fetched.
    page_results = {cursor: first_collection}

    # Pages that never came back. A failed page is silently an empty page here,
    # so without this the run would write a short CSV and still exit 0 — the
    # orchestrator would publish the truncated result as a successful scrape.
    fetch_failures = 0

    # ------------------------------------------------------------------
    # Step 2: work out the remaining offsets and fan them out in parallel.
    # ------------------------------------------------------------------
    if total_records is not None:
        end_offset = total_records
        if limit and limit > 0:
            # Round the limit up to a full page so threaded / sequential fetch
            # exactly the same offset set (guarantees identical resulting data).
            pages_for_limit = (limit + PAGE_SIZE - 1) // PAGE_SIZE
            end_offset = min(end_offset, cursor + pages_for_limit * PAGE_SIZE)
        offsets = list(range(cursor + PAGE_SIZE, end_offset, PAGE_SIZE))

        if offsets:
            logger.info(f"Total in range: {total_records}. Fetching {len(offsets)} additional page(s).")
            if threads > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
                    future_to_off = {
                        executor.submit(fetch_api_page, server, start_date, end_date, off, delay=delay): off
                        for off in offsets
                    }
                    for future in concurrent.futures.as_completed(future_to_off):
                        off = future_to_off[future]
                        try:
                            d = future.result()
                            page_results[off] = d.get("collection", []) or []
                            logger.debug(f"  Fetched offset {off}: {len(page_results[off])} records.")
                        except Exception as e:
                            logger.error(f"Error fetching offset {off}: {e}")
                            page_results[off] = []
                            fetch_failures += 1
            else:
                # Sequential path (--threads 1); still rate-limited.
                for off in offsets:
                    try:
                        d = fetch_api_page(server, start_date, end_date, off, delay=delay)
                        page_results[off] = d.get("collection", []) or []
                    except Exception as e:
                        logger.error(f"Error fetching offset {off}: {e}")
                        page_results[off] = []
                        fetch_failures += 1
    else:
        # Fallback: total unknown -> walk pages sequentially (rate-limited) until empty.
        logger.info("Total unknown for this range; walking pages sequentially until exhausted.")
        off = cursor + PAGE_SIZE
        while True:
            if limit and sum(len(v) for v in page_results.values()) >= (limit + PAGE_SIZE):
                break
            try:
                d = fetch_api_page(server, start_date, end_date, off, delay=delay)
            except Exception as e:
                logger.error(f"Error fetching offset {off}: {e}")
                fetch_failures += 1
                break
            coll = d.get("collection", []) or []
            if not coll:
                break
            page_results[off] = coll
            off += PAGE_SIZE

    # ------------------------------------------------------------------
    # Step 3: combine (in deterministic offset order), de-duplicate, sort, write.
    # ------------------------------------------------------------------
    all_items = []
    total_fetched = 0
    for off in sorted(page_results):
        coll = page_results[off]
        all_items.extend(coll)
        total_fetched += len(coll)

    rows = []
    seen_this_run = set()
    for item in all_items:
        doi = item.get("doi", "").strip()
        if not doi:
            continue
        if doi in completed_dois or doi in seen_this_run:
            continue
        seen_this_run.add(doi)
        rows.append(_row_from_item(item, server))
        if limit and len(rows) >= limit:
            break

    # Parallel fetches complete out of order -> sort deterministically before writing
    # so threaded and sequential runs always produce byte-identical output.
    rows.sort(key=lambda r: (r.get("date", ""), r.get("doi", ""), str(r.get("version", ""))))

    mode = "a" if meta_csv_exists else "w"
    with open(metadata_csv_file, mode, newline="", encoding="utf-8") as f_csv:
        csv_writer = csv.DictWriter(f_csv, fieldnames=csv_fields)
        if not meta_csv_exists:
            csv_writer.writeheader()
        for row in rows:
            csv_writer.writerow(row)

    # Update counts and cursor for a future resume.
    processed_count += len(rows)
    pages_count += len(page_results)
    next_cursor = cursor + total_fetched

    # Save progress JSON
    with open(progress_file, "w", encoding="utf-8") as pf:
        json.dump({
            "server": server,
            "start_date": start_date,
            "end_date": end_date,
            "next_cursor": next_cursor,
            "total_records_processed": processed_count,
            "total_pages_processed": pages_count,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }, pf, indent=2)

    elapsed = time.monotonic() - session_start
    total_target = f"/{total_records}" if total_records else ""
    logger.info(
        f"Wrote {len(rows)} new record(s) from {len(page_results)} page(s) in {elapsed:.2f}s. "
        f"Cursor now {next_cursor}. Progress: {processed_count}{total_target}."
    )
    logger.info(f"Session finished. Total {server} records processed in this session: {len(rows)}.")
    return fetch_failures


def main():
    # Set default date range (last 30 days)
    today = datetime.date.today()
    default_start = (today - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
    default_end = today.strftime("%Y-%m-%d")

    parser = argparse.ArgumentParser(
        description="Scrape preprint metadata from Cold Spring Harbor Laboratory (CSHL) bioRxiv API."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "biorxiv"),
        help="Directory to save output CSV datasets and progress tracking logs."
    )
    parser.add_argument(
        "--start-date",
        type=parse_date,
        default=default_start,
        help="Start date for preprint publication search (YYYY-MM-DD). Defaults to 30 days ago."
    )
    parser.add_argument(
        "--end-date",
        type=parse_date,
        default=default_end,
        help="End date for preprint publication search (YYYY-MM-DD). Defaults to today."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Total maximum number of works to download in this execution session (useful for testing)."
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=5,
        help="Number of parallel worker threads for page fetches (default: 5; use 1 for sequential). "
             "The combined request rate across all threads is still capped at "
             f"{MAX_REQUESTS_PER_SECOND} req/s."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Base back-off delay in seconds used for retries (default: 1.0s)."
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore previous progress and run a clean search crawl."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enables full debug logging output."
    )

    args = parser.parse_args()

    threads = max(1, args.threads)

    output_path = Path(args.output_dir)
    log_file = output_path / "biorxiv_downloader.log"
    setup_logging(args.verbose, log_file)

    failures = run_downloader(
        server="biorxiv",
        output_dir=output_path,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        delay=args.delay,
        fresh=args.fresh,
        threads=threads
    )
    # Fail loudly on a partial crawl so the pipeline does not publish it as complete.
    if failures:
        logger.error(f"{failures} page fetch(es) failed — crawl is incomplete.")
        sys.exit(1)


if __name__ == "__main__":
    main()
