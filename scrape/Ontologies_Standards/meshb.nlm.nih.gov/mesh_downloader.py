#!/usr/bin/env python3
"""
Medical Subject Headings (MeSH) Downloader and Parser
Author: Antigravity AI Coding Assistant
Description: Connects to the official U.S. National Library of Medicine (NLM) FTP / web server
             to retrieve the latest MeSH descriptors, qualifiers, pharmacological actions, and
             optionally supplemental concept records. It processes these heavy XML databases
             using a streaming ElementTree parser to convert them into structured, easy-to-use CSVs.
"""

import os
import sys
import zipfile
import csv
import argparse
import logging
import requests
import xml.etree.ElementTree as ET
from pathlib import Path

# Resolve all output paths relative to this script's folder (pipeline contract)
BASE_DIR = Path(__file__).resolve().parent

# HTTP headers to play nice with NLM servers
HEADERS = {
    "User-Agent": "BiolytInternMeSHDownloader/1.0 (Python; requests; Antigravity)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Logger setup
logger = logging.getLogger("mesh_downloader")


def setup_logging(verbose: bool):
    """Configures the logging output level."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )


def download_file(url: str, dest_path: Path) -> bool:
    """Downloads a file from a URL with basic progress logging."""
    logger.info(f"Downloading: {url}")
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=60)
        if response.status_code == 404:
            logger.error(f"File not found (404) at: {url}. Please check if the year is correct.")
            return False
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        chunk_size = 1024 * 1024  # 1 MB chunking
        downloaded = 0

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        logger.info(f"  Downloaded {downloaded / (1024*1024):.1f} MB / {total_size / (1024*1024):.1f} MB ({percent:.1f}%)")
                    else:
                        logger.info(f"  Downloaded {downloaded / (1024*1024):.1f} MB")
        
        logger.info(f"Successfully downloaded to: {dest_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to download from {url}. Error: {e}")
        return False


def unzip_file(zip_path: Path, extract_to: Path):
    """Unzips a ZIP archive to a destination path."""
    logger.info(f"Extracting: {zip_path} -> {extract_to}")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(extract_to)
    logger.info("Extraction complete.")


def parse_descriptors(xml_path: Path, csv_path: Path):
    """Parses desc{year}.xml using a streaming XML parser and saves to CSV."""
    logger.info(f"Parsing Descriptors from: {xml_path}")
    count = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "descriptor_ui", 
            "name", 
            "scope_note", 
            "tree_numbers", 
            "registry_number", 
            "synonyms", 
            "pharmacological_actions"
        ])

        # Using iterparse to stream XML without holding the whole tree in RAM
        context = ET.iterparse(xml_path, events=("end",))
        
        for event, elem in context:
            if elem.tag == "DescriptorRecord":
                descriptor_ui = elem.findtext("DescriptorUI")
                
                # Extract Descriptor Name
                name_node = elem.find(".//DescriptorName/String")
                descriptor_name = name_node.text.strip() if name_node is not None and name_node.text else ""

                if not descriptor_ui or not descriptor_name:
                    elem.clear()
                    continue

                scope_note = ""
                registry_number = ""
                synonyms = set()

                # Extract Preferred Concept Info and Synonym Terms
                concept_list = elem.find("ConceptList")
                if concept_list is not None:
                    for concept in concept_list.findall("Concept"):
                        is_preferred = concept.attrib.get("PreferredConceptYN") == "Y"
                        
                        if is_preferred:
                            # Scope Note
                            sn_node = concept.find("ScopeNote")
                            if sn_node is not None and sn_node.text:
                                scope_note = sn_node.text.strip()

                            # Registry Number (UNII/CAS)
                            reg_num_list = concept.find("RegistryNumberList")
                            if reg_num_list is not None:
                                reg_num = reg_num_list.find("RegistryNumber")
                                if reg_num is not None and reg_num.text:
                                    registry_number = reg_num.text.strip()

                        # Collect Terms (Synonyms)
                        term_list = concept.find("TermList")
                        if term_list is not None:
                            for term in term_list.findall("Term"):
                                term_string_node = term.find("String")
                                if term_string_node is not None and term_string_node.text:
                                    term_str = term_string_node.text.strip()
                                    if term_str and term_str.lower() != descriptor_name.lower():
                                        synonyms.add(term_str)

                # Extract Tree Numbers
                tree_numbers = []
                tree_num_list = elem.find("TreeNumberList")
                if tree_num_list is not None:
                    for tn in tree_num_list.findall("TreeNumber"):
                        if tn.text:
                            tree_numbers.append(tn.text.strip())

                # Extract Pharmacological Actions
                pa_actions = []
                pa_list_node = elem.find("PharmacologicalActionList")
                if pa_list_node is not None:
                    for pa in pa_list_node.findall("PharmacologicalAction"):
                        referred = pa.find("DescriptorReferredTo")
                        if referred is not None:
                            pa_name_node = referred.find("DescriptorName/String")
                            if pa_name_node is not None and pa_name_node.text:
                                pa_actions.append(pa_name_node.text.strip())

                # Write parsed record to CSV
                writer.writerow([
                    descriptor_ui,
                    descriptor_name,
                    scope_note,
                    ";".join(tree_numbers),
                    registry_number,
                    ";".join(sorted(synonyms)),
                    ";".join(sorted(pa_actions))
                ])

                count += 1
                if count % 5000 == 0:
                    logger.info(f"  Processed {count} Descriptor records...")

                # Clean up element memory
                elem.clear()

    logger.info(f"Completed Descriptors. Saved {count} records -> {csv_path}")


def parse_qualifiers(xml_path: Path, csv_path: Path):
    """Parses qual{year}.xml using a streaming XML parser and saves to CSV."""
    logger.info(f"Parsing Qualifiers from: {xml_path}")
    count = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["qualifier_ui", "name", "abbreviation", "scope_note", "tree_numbers"])

        context = ET.iterparse(xml_path, events=("end",))
        
        for event, elem in context:
            if elem.tag == "QualifierRecord":
                qualifier_ui = elem.findtext("QualifierUI")
                
                name_node = elem.find(".//QualifierName/String")
                qualifier_name = name_node.text.strip() if name_node is not None and name_node.text else ""

                if not qualifier_ui or not qualifier_name:
                    elem.clear()
                    continue

                abbreviation = ""
                scope_note = ""

                # Extract abbreviation & scope note
                concept_list = elem.find("ConceptList")
                if concept_list is not None:
                    for concept in concept_list.findall("Concept"):
                        is_preferred = concept.attrib.get("PreferredConceptYN") == "Y"
                        
                        if is_preferred:
                            sn_node = concept.find("ScopeNote")
                            if sn_node is not None and sn_node.text:
                                scope_note = sn_node.text.strip()

                        term_list = concept.find("TermList")
                        if term_list is not None:
                            for term in term_list.findall("Term"):
                                abbr_node = term.find("Abbreviation")
                                if abbr_node is not None and abbr_node.text:
                                    abbreviation = abbr_node.text.strip()
                                    break
                        if abbreviation and scope_note:
                            # Break concept loop early if we found both from preferred
                            if is_preferred:
                                break

                # Extract Tree Numbers
                tree_numbers = []
                tree_num_list = elem.find("TreeNumberList")
                if tree_num_list is not None:
                    for tn in tree_num_list.findall("TreeNumber"):
                        if tn.text:
                            tree_numbers.append(tn.text.strip())

                # Write to CSV
                writer.writerow([
                    qualifier_ui,
                    qualifier_name,
                    abbreviation,
                    scope_note,
                    ";".join(tree_numbers)
                ])

                count += 1
                elem.clear()

    logger.info(f"Completed Qualifiers. Saved {count} records -> {csv_path}")


def parse_pharmacological_actions(xml_path: Path, csv_path: Path):
    """Parses pa{year}.xml using a streaming XML parser and saves to CSV."""
    logger.info(f"Parsing Pharmacological Actions from: {xml_path}")
    count = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["action_descriptor_ui", "action_name", "substance_record_ui", "substance_name", "substance_type"])

        context = ET.iterparse(xml_path, events=("end",))
        
        for event, elem in context:
            if elem.tag == "PharmacologicalAction":
                ref = elem.find("DescriptorReferredTo")
                if ref is None:
                    elem.clear()
                    continue

                action_ui = ref.findtext("DescriptorUI")
                action_name_node = ref.find("DescriptorName/String")
                action_name = action_name_node.text.strip() if action_name_node is not None and action_name_node.text else ""

                if not action_ui or not action_name:
                    elem.clear()
                    continue

                # Process mapped substances
                substance_list = elem.find("PharmacologicalActionSubstanceList")
                if substance_list is not None:
                    for substance in substance_list.findall("Substance"):
                        sub_ui = substance.findtext("RecordUI")
                        sub_name_node = substance.find("RecordName/String")
                        sub_name = sub_name_node.text.strip() if sub_name_node is not None and sub_name_node.text else ""

                        if sub_ui and sub_name:
                            # Determine type: starts with D (Descriptor), starts with C (Supplemental Concept)
                            sub_type = "Descriptor" if sub_ui.startswith("D") else "Supplemental"
                            
                            writer.writerow([
                                action_ui,
                                action_name,
                                sub_ui,
                                sub_name,
                                sub_type
                            ])
                            count += 1

                elem.clear()

    logger.info(f"Completed Pharmacological Actions. Saved {count} mappings -> {csv_path}")


def parse_supplemental_concepts(xml_path: Path, csv_path: Path):
    """Parses supp{year}.xml using a streaming XML parser and saves to CSV."""
    logger.info(f"Parsing Supplemental Concept Records (SCR) from: {xml_path}")
    count = 0

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "supplemental_ui", 
            "name", 
            "registry_number", 
            "heading_mapped_to_uis", 
            "heading_mapped_to_names", 
            "synonyms"
        ])

        context = ET.iterparse(xml_path, events=("end",))
        
        for event, elem in context:
            if elem.tag == "SupplementalRecord":
                supp_ui = elem.findtext("SupplementalRecordUI")
                
                name_node = elem.find(".//SupplementalRecordName/String")
                supp_name = name_node.text.strip() if name_node is not None and name_node.text else ""

                if not supp_ui or not supp_name:
                    elem.clear()
                    continue

                registry_number = ""
                synonyms = set()

                # Extract Preferred Concept Info and Synonym Terms
                concept_list = elem.find("ConceptList")
                if concept_list is not None:
                    for concept in concept_list.findall("Concept"):
                        is_preferred = concept.attrib.get("PreferredConceptYN") == "Y"
                        
                        if is_preferred:
                            reg_num_list = concept.find("RegistryNumberList")
                            if reg_num_list is not None:
                                reg_num = reg_num_list.find("RegistryNumber")
                                if reg_num is not None and reg_num.text:
                                    registry_number = reg_num.text.strip()

                        # Collect Terms (Synonyms)
                        term_list = concept.find("TermList")
                        if term_list is not None:
                            for term in term_list.findall("Term"):
                                term_string_node = term.find("String")
                                if term_string_node is not None and term_string_node.text:
                                    term_str = term_string_node.text.strip()
                                    if term_str and term_str.lower() != supp_name.lower():
                                        synonyms.add(term_str)

                # Extract Mapped Headings (HM)
                mapped_uis = []
                mapped_names = []
                hm_list = elem.find("HeadingMappedToList")
                if hm_list is not None:
                    for hm in hm_list.findall("HeadingMappedTo"):
                        ref = hm.find("DescriptorReferredTo")
                        if ref is not None:
                            ui = ref.findtext("DescriptorUI")
                            mname_node = ref.find("DescriptorName/String")
                            if ui:
                                mapped_uis.append(ui.strip())
                            if mname_node is not None and mname_node.text:
                                mapped_names.append(mname_node.text.strip())

                # Write parsed record to CSV
                writer.writerow([
                    supp_ui,
                    supp_name,
                    registry_number,
                    ";".join(mapped_uis),
                    ";".join(mapped_names),
                    ";".join(sorted(synonyms))
                ])

                count += 1
                if count % 20000 == 0:
                    logger.info(f"  Processed {count} SCR records...")

                elem.clear()

    logger.info(f"Completed Supplemental Concept Records. Saved {count} records -> {csv_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape and parse NLM Medical Subject Headings (MeSH) XML files into structured CSVs."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(BASE_DIR / "mesh_data"),
        help="Directory to save downloaded files and CSV outputs (default: Ontologies & Standards/mesh_data)"
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2026,
        help="MeSH Production Year to download (default: 2026)"
    )
    parser.add_argument(
        "--include-scr",
        action="store_true",
        help="Include downloading and parsing Supplemental Concept Records (SCR). WARNING: Large download/dataset."
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Keep raw XML and ZIP files after processing (default: False/cleanup)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose DEBUG logging"
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    logger.info("=" * 60)
    logger.info("         NLM MeSH DATA COLLECTION PROCESS")
    logger.info("=" * 60)
    logger.info(f"Production Year: {args.year}")
    logger.info(f"Include SCRs:    {args.include_scr}")
    logger.info(f"Keep Raw Files:  {args.keep_raw}")

    # Ensure output directory exists
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Target Output Directory: {out_dir.resolve()}")

    # Track files to delete later (if not keeping raw)
    temp_files = []

    # 1. Process Descriptors
    logger.info("\n--- Processing Descriptors ---")
    desc_zip_url = f"https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc{args.year}.zip"
    desc_zip_path = out_dir / f"desc{args.year}.zip"
    desc_xml_path = out_dir / f"desc{args.year}.xml"
    desc_csv_path = out_dir / "mesh_descriptors.csv"

    # Only download/unzip if CSV doesn't exist, or if run fresh
    if not desc_csv_path.exists():
        if download_file(desc_zip_url, desc_zip_path):
            temp_files.append(desc_zip_path)
            unzip_file(desc_zip_path, out_dir)
            temp_files.append(desc_xml_path)
            parse_descriptors(desc_xml_path, desc_csv_path)
    else:
        logger.info(f"Skipping Descriptors - CSV already exists at: {desc_csv_path}")

    # 2. Process Qualifiers
    logger.info("\n--- Processing Qualifiers ---")
    qual_url = f"https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/qual{args.year}.xml"
    qual_xml_path = out_dir / f"qual{args.year}.xml"
    qual_csv_path = out_dir / "mesh_qualifiers.csv"

    if not qual_csv_path.exists():
        if download_file(qual_url, qual_xml_path):
            temp_files.append(qual_xml_path)
            parse_qualifiers(qual_xml_path, qual_csv_path)
    else:
        logger.info(f"Skipping Qualifiers - CSV already exists at: {qual_csv_path}")

    # 3. Process Pharmacological Actions
    logger.info("\n--- Processing Pharmacological Actions ---")
    pa_url = f"https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/pa{args.year}.xml"
    pa_xml_path = out_dir / f"pa{args.year}.xml"
    pa_csv_path = out_dir / "mesh_pharmacological_actions.csv"

    if not pa_csv_path.exists():
        if download_file(pa_url, pa_xml_path):
            temp_files.append(pa_xml_path)
            parse_pharmacological_actions(pa_xml_path, pa_csv_path)
    else:
        logger.info(f"Skipping Pharmacological Actions - CSV already exists at: {pa_csv_path}")

    # 4. Process Supplemental Concept Records (if requested)
    if args.include_scr:
        logger.info("\n--- Processing Supplemental Concept Records (SCR) ---")
        supp_zip_url = f"https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/supp{args.year}.zip"
        supp_zip_path = out_dir / f"supp{args.year}.zip"
        supp_xml_path = out_dir / f"supp{args.year}.xml"
        supp_csv_path = out_dir / "mesh_supplemental_concepts.csv"

        if not supp_csv_path.exists():
            if download_file(supp_zip_url, supp_zip_path):
                temp_files.append(supp_zip_path)
                unzip_file(supp_zip_path, out_dir)
                temp_files.append(supp_xml_path)
                parse_supplemental_concepts(supp_xml_path, supp_csv_path)
        else:
            logger.info(f"Skipping SCRs - CSV already exists at: {supp_csv_path}")
    else:
        logger.info("\nSupplemental Concept Records (SCR) skipped (use --include-scr to compile).")

    # 5. Cleanup raw files
    if not args.keep_raw:
        logger.info("\n--- Cleaning up temporary XML & ZIP files ---")
        for fpath in temp_files:
            if fpath.exists():
                try:
                    fpath.unlink()
                    logger.info(f"  Deleted temporary file: {fpath.name}")
                except Exception as e:
                    logger.warning(f"  Could not delete temporary file {fpath}: {e}")
    else:
        logger.info("\n--- Keeping raw XML & ZIP files as requested ---")

    # Previews using pandas if available
    try:
        import pandas as pd
        logger.info("\n" + "=" * 60)
        logger.info("           OUTPUT DATASETS PREVIEW")
        logger.info("=" * 60)
        
        if desc_csv_path.exists():
            df_desc = pd.read_csv(desc_csv_path)
            logger.info(f"\nDescriptors ({len(df_desc)} records) - First 3 rows:")
            logger.info(df_desc[["descriptor_ui", "name", "registry_number"]].head(3).to_string())

        if qual_csv_path.exists():
            df_qual = pd.read_csv(qual_csv_path)
            logger.info(f"\nQualifiers ({len(df_qual)} records) - First 3 rows:")
            logger.info(df_qual[["qualifier_ui", "name", "abbreviation"]].head(3).to_string())

        if pa_csv_path.exists():
            df_pa = pd.read_csv(pa_csv_path)
            logger.info(f"\nPharmacological Actions ({len(df_pa)} mappings) - First 3 rows:")
            logger.info(df_pa.head(3).to_string())

        if args.include_scr and Path(out_dir / "mesh_supplemental_concepts.csv").exists():
            df_supp = pd.read_csv(out_dir / "mesh_supplemental_concepts.csv")
            logger.info(f"\nSupplemental Concepts ({len(df_supp)} records) - First 3 rows:")
            logger.info(df_supp[["supplemental_ui", "name", "registry_number"]].head(3).to_string())
            
    except ImportError:
        logger.info("\n(Install pandas to see inline previews of the saved files)")

    logger.info("\n" + "=" * 60)
    logger.info("              MeSH DOWNLOAD COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
