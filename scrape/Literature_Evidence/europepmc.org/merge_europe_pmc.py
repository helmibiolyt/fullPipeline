#!/usr/bin/env python3
"""
Europe PMC Data Merger
Author: Antigravity AI Coding Assistant
Description: Merges the metadata and LLM-extracted full-text CSVs of Europe PMC data
             intelligently, performing data cleaning, normalization, and generating useful stats.
"""

import os
import sys
import csv
import json
import argparse
from pathlib import Path
import pandas as pd
import numpy as np

# Resolve all paths relative to this script, never the current working directory.
BASE_DIR = Path(__file__).resolve().parent

def clean_none_values(val):
    """Converts string 'None' (case-insensitive) or equivalent placeholders to standard numpy NaN."""
    if pd.isna(val):
        return np.nan
    if isinstance(val, str):
        val_strip = val.strip()
        if val_strip.lower() in ("none", "null", "nan", ""):
            return np.nan
        return val_strip
    return val

def clean_boolean(val):
    """Normalizes boolean-like fields to True/False/None."""
    if pd.isna(val):
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    val_str = str(val).strip().lower()
    if val_str in ("y", "yes", "true", "1", "t"):
        return True
    if val_str in ("n", "no", "false", "0", "f"):
        return False
    return None

def clean_integer(val):
    """Safely converts values to integer or returns None."""
    if pd.isna(val):
        return None
    try:
        # Handle cases like "123.0" or " 123 "
        return int(float(val))
    except (ValueError, TypeError):
        return None

def clean_enrollment(val):
    """Clean enrollment values which can be float, int, or string representations."""
    if pd.isna(val):
        return None
    val_str = str(val).strip().lower()
    if val_str in ("none", "null", "nan", ""):
        return None
    # Try to extract the first consecutive digits if it is a messy string (e.g. "120 patients")
    import re
    match = re.search(r'\d+', val_str)
    if match:
        return int(match.group())
    return None

def merge_and_clean_data(metadata_path, full_text_path, output_path, merge_strategy="left", clean_none=True):
    print(f"Reading metadata from: {metadata_path}")
    df_meta = pd.read_csv(metadata_path)
    
    print(f"Reading full-text data from: {full_text_path}")
    df_ft = pd.read_csv(full_text_path)
    
    print(f"Original shapes -> Metadata: {df_meta.shape}, Full-Text: {df_ft.shape}")
    
    # Check uniqueness of 'id'
    meta_dups = df_meta.duplicated(subset=['id']).sum()
    ft_dups = df_ft.duplicated(subset=['id']).sum()
    if meta_dups > 0:
        print(f"[WARNING] Found {meta_dups} duplicate IDs in metadata. Deduplicating and keeping the first occurrence.")
        df_meta = df_meta.drop_duplicates(subset=['id'], keep='first')
    if ft_dups > 0:
        print(f"[WARNING] Found {ft_dups} duplicate IDs in full-text. Deduplicating and keeping the first occurrence.")
        df_ft = df_ft.drop_duplicates(subset=['id'], keep='first')

    # Merge the dataframes on 'id'
    print(f"Merging data using strategy: '{merge_strategy}' on key: 'id'")
    merged_df = pd.merge(df_meta, df_ft, on="id", how=merge_strategy, suffixes=("_meta", "_ft"))
    
    # Apply standard cleanups
    print("Performing intelligent data cleaning and normalization...")
    
    # 1. Clean "None" string placeholders from ALL columns if requested
    if clean_none:
        for col in merged_df.columns:
            if col != 'id':
                merged_df[col] = merged_df[col].apply(clean_none_values)

    # 2. Standardize boolean columns
    bool_cols = ["is_open_access", "has_pdf"]
    for col in bool_cols:
        if col in merged_df.columns:
            merged_df[col] = merged_df[col].apply(clean_boolean)

    # 3. Standardize integer columns
    int_cols = ["cited_by_count", "pub_year"]
    for col in int_cols:
        if col in merged_df.columns:
            merged_df[col] = merged_df[col].apply(clean_integer)

    # 4. Clean enrollment field
    if "enrollment" in merged_df.columns:
        merged_df["enrollment"] = merged_df["enrollment"].apply(clean_enrollment)

    # 5. Intelligent Fallbacks
    # Fallback: if brief_title or official_title is empty, fall back to metadata title
    if "brief_title" in merged_df.columns and "title" in merged_df.columns:
        merged_df["brief_title"] = merged_df["brief_title"].fillna(merged_df["title"])
    if "official_title" in merged_df.columns and "title" in merged_df.columns:
        merged_df["official_title"] = merged_df["official_title"].fillna(merged_df["title"])

    # 6. Normalize NCT IDs (ClinicalTrials.gov identifier)
    if "nct_id" in merged_df.columns:
        # Standardize NCT ID format (e.g. NCT12345678 or similar)
        # Strip trailing/leading spaces, verify if it starts with NCT followed by 8 digits
        import re
        def normalize_nct(val):
            if pd.isna(val) or not isinstance(val, str):
                return np.nan
            val_clean = val.strip().upper()
            match = re.search(r'NCT\d{8}', val_clean)
            if match:
                return match.group()
            return np.nan
        merged_df["nct_id"] = merged_df["nct_id"].apply(normalize_nct)

    # 7. Standardize string/array fields (trim whitespace)
    str_cols = [
        "authors", "author_affiliations", "journal_title", "pub_type", 
        "condition", "phase", "intervention_name", "intervention_type", 
        "primary_outcome", "secondary_outcome", "country", "study_type", 
        "medicine_name", "active_substance"
    ]
    for col in str_cols:
        if col in merged_df.columns:
            merged_df[col] = merged_df[col].apply(lambda x: str(x).strip() if pd.notna(x) and not isinstance(x, (int, float)) else np.nan)

    # Save to output path
    print(f"Saving merged and cleaned data to: {output_path}")
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    merged_df.to_csv(output_path, index=False)
    
    print("\n" + "="*50)
    print("MERGE & CLEANING STATISTICS")
    print("="*50)
    total_records = len(merged_df)
    print(f"Total merged records: {total_records}")
    
    if "extracted_at" in merged_df.columns:
        records_with_ft = merged_df["extracted_at"].notna().sum()
        print(f"Records with full-text/LLM data: {records_with_ft} ({records_with_ft/total_records*100:.1f}%)")
        print(f"Records missing full-text data: {total_records - records_with_ft} ({(total_records - records_with_ft)/total_records*100:.1f}%)")

    if "nct_id" in merged_df.columns:
        has_nct = merged_df["nct_id"].notna().sum()
        print(f"Clinical trial records (with NCT ID): {has_nct} ({has_nct/total_records*100:.1f}%)")

    if "pub_year" in merged_df.columns:
        year_counts = merged_df["pub_year"].dropna().astype(int).value_counts().head(5)
        print("\nTop 5 Publication Years:")
        for yr, count in year_counts.items():
            print(f"  - {yr}: {count} articles")

    if "phase" in merged_df.columns:
        phase_counts = merged_df["phase"].dropna().value_counts().head(5)
        if not phase_counts.empty:
            print("\nTop Trial Phases Extracted:")
            for ph, count in phase_counts.items():
                print(f"  - {ph}: {count} articles")
                
    print("="*50)

def main():
    parser = argparse.ArgumentParser(
        description="Merge Europe PMC metadata and full text extraction CSVs intelligently."
    )
    parser.add_argument(
        "--metadata-csv",
        type=str,
        default=str(BASE_DIR / "europe_pmc" / "europe_pmc_metadata.csv"),
        help="Path to the metadata CSV file."
    )
    parser.add_argument(
        "--full-text-csv",
        type=str,
        default=str(BASE_DIR / "europe_pmc" / "europe_pmc_full_text.csv"),
        help="Path to the full-text extracted CSV file."
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default=str(BASE_DIR / "europe_pmc" / "europe_pmc_merged_clean.csv"),
        help="Path where the merged CSV file will be saved."
    )
    parser.add_argument(
        "--merge-strategy",
        type=str,
        default="left",
        choices=["left", "right", "inner", "outer"],
        help="The merge strategy (join type) to use (default: left)."
    )
    parser.add_argument(
        "--keep-none-strings",
        action="store_true",
        help="Keep literal 'None' string values from the extraction instead of converting them to blanks."
    )
    
    args = parser.parse_args()
    
    # Check file existence
    if not os.path.exists(args.metadata_csv):
        print(f"Error: Metadata file not found at '{args.metadata_csv}'")
        sys.exit(1)
    if not os.path.exists(args.full_text_csv):
        print(f"Error: Full-text file not found at '{args.full_text_csv}'")
        sys.exit(1)
            
    merge_and_clean_data(
        metadata_path=args.metadata_csv,
        full_text_path=args.full_text_csv,
        output_path=args.output_csv,
        merge_strategy=args.merge_strategy,
        clean_none=not args.keep_none_strings
    )

if __name__ == "__main__":
    main()
