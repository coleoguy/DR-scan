#!/usr/bin/env python3
"""
download_insect_genomes.py
==========================
Downloads up to 5 high-quality (chromosome-level) genome assemblies per
insect order from NCBI, along with paired GFF3 annotation files when
available.

Uses the NCBI Datasets CLI (v2) for surveying and downloading. The CLI
is auto-installed to ./bin/ if not already on the system PATH.

Output structure:
    genomes/
      <Order>/
        <Accession>/
          *.fna        (genome FASTA)
          *.gff        (GFF3 annotation, if available)
      summary.tsv      (global summary of all downloaded genomes)

Usage:
    python3 scripts/download_insect_genomes.py

Author: Heath Blackmon lab / generated with Claude Code
Date:   2026-04-06
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_GENOMES_PER_ORDER = 5          # Max assemblies to download per order
ASSEMBLY_LEVELS = "chromosome,complete"  # Only high-quality assemblies
INCLUDE_DATA = "genome,gff3"       # Download genome FASTA + GFF3 annotation
OUTPUT_DIR = Path("genomes")       # Root output directory
BIN_DIR = Path("bin")              # Local directory for NCBI CLI tools
RETRY_ATTEMPTS = 3                 # Number of retries on failure
DOWNLOAD_TIMEOUT = 1800            # 30-minute timeout per download (seconds)
SURVEY_TIMEOUT = 120               # 2-minute timeout per survey call
DELAY_BETWEEN_ORDERS = 2           # Seconds to pause between order queries

# All recognized insect orders. NCBI will return zero results for any
# taxon name it does not recognize, so it is safe to include all.
INSECT_ORDERS = [
    "Archaeognatha",    # Bristletails
    "Blattodea",        # Cockroaches and termites (includes former Isoptera)
    "Coleoptera",       # Beetles
    "Dermaptera",       # Earwigs
    "Diptera",          # Flies, mosquitoes
    "Embioptera",       # Webspinners
    "Ephemeroptera",    # Mayflies
    "Grylloblattodea",  # Ice crawlers
    "Hemiptera",        # True bugs, aphids, cicadas
    "Hymenoptera",      # Ants, bees, wasps
    "Lepidoptera",      # Butterflies and moths
    "Mantodea",         # Mantises
    "Mantophasmatodea", # Gladiators
    "Mecoptera",        # Scorpionflies
    "Megaloptera",      # Dobsonflies, alderflies
    "Neuroptera",       # Lacewings, antlions
    "Odonata",          # Dragonflies and damselflies
    "Orthoptera",       # Grasshoppers, crickets
    "Phasmatodea",      # Stick insects
    "Phthiraptera",     # Lice
    "Plecoptera",       # Stoneflies
    "Psocodea",         # Bark lice and true lice (modern grouping)
    "Raphidioptera",    # Snakeflies
    "Siphonaptera",     # Fleas
    "Strepsiptera",     # Twisted-wing parasites
    "Thysanoptera",     # Thrips
    "Trichoptera",      # Caddisflies
    "Zoraptera",        # Angel insects
    "Zygentoma",        # Silverfish
]

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging():
    """Configure logging to both console (INFO) and file (DEBUG)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = OUTPUT_DIR / "download.log"

    # Root logger at DEBUG level
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Console handler: INFO and above
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    root_logger.addHandler(console)

    # File handler: DEBUG and above (full detail)
    fh = logging.FileHandler(log_file, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root_logger.addHandler(fh)

    return root_logger


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NCBI Datasets CLI management
# ---------------------------------------------------------------------------

def get_env_with_bin():
    """Return a copy of os.environ with ./bin/ prepended to PATH."""
    env = os.environ.copy()
    bin_abs = str(BIN_DIR.resolve())
    env["PATH"] = bin_abs + os.pathsep + env.get("PATH", "")
    return env


def ensure_datasets_cli():
    """
    Check if the NCBI 'datasets' CLI is available. If not, download the
    appropriate binary for the current platform into ./bin/.
    """
    env = get_env_with_bin()

    # Check if already available
    if shutil.which("datasets", path=env["PATH"]):
        version = subprocess.run(
            ["datasets", "--version"], capture_output=True, text=True, env=env
        )
        logger.info(f"NCBI datasets CLI found: {version.stdout.strip()}")
        return

    logger.info("NCBI datasets CLI not found — downloading...")
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    # Determine platform-specific download URLs
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        base_url = "https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/mac"
    elif system == "linux":
        base_url = "https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/linux-amd64"
    else:
        logger.error(f"Unsupported platform: {system} {machine}")
        sys.exit(1)

    # Download both 'datasets' and 'dataformat' binaries
    for tool_name in ["datasets", "dataformat"]:
        url = f"{base_url}/{tool_name}"
        dest = BIN_DIR / tool_name
        logger.info(f"  Downloading {tool_name} from {url}")
        subprocess.run(
            ["curl", "-L", "-o", str(dest), url],
            check=True, timeout=300
        )
        dest.chmod(0o755)

    # Verify installation
    version = subprocess.run(
        ["datasets", "--version"], capture_output=True, text=True, env=env
    )
    if version.returncode == 0:
        logger.info(f"  Successfully installed: {version.stdout.strip()}")
    else:
        logger.error("Failed to install NCBI datasets CLI")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Survey: query NCBI for available assemblies per order
# ---------------------------------------------------------------------------

def survey_order(order_name):
    """
    Query NCBI for all chromosome/complete-level assemblies in a given
    insect order. Returns a list of assembly record dicts.
    """
    env = get_env_with_bin()
    cmd = [
        "datasets", "summary", "genome", "taxon", order_name,
        "--assembly-level", ASSEMBLY_LEVELS,
        "--as-json-lines",
    ]
    logger.debug(f"Running: {' '.join(cmd)}")

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                env=env, timeout=SURVEY_TIMEOUT
            )
            break
        except subprocess.TimeoutExpired:
            logger.warning(
                f"  Survey timeout for {order_name} (attempt {attempt}/{RETRY_ATTEMPTS})"
            )
            if attempt == RETRY_ATTEMPTS:
                logger.error(f"  Survey failed for {order_name} after {RETRY_ATTEMPTS} attempts")
                return []
            time.sleep(2 ** attempt)
        except subprocess.CalledProcessError as e:
            logger.warning(f"  Survey error for {order_name}: {e}")
            if attempt == RETRY_ATTEMPTS:
                return []
            time.sleep(2 ** attempt)

    # Parse JSON-lines output — each line is one assembly record
    assemblies = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            # The JSON-lines format wraps assemblies in a 'reports' key
            # when there's a single top-level object, or each line is a
            # report directly. Handle both cases.
            if "reports" in record:
                assemblies.extend(record["reports"])
            else:
                assemblies.append(record)
        except json.JSONDecodeError:
            logger.debug(f"  Skipping unparseable line: {line[:100]}")
            continue

    logger.info(f"  {order_name}: found {len(assemblies)} chromosome-level assemblies")
    return assemblies


# ---------------------------------------------------------------------------
# Ranking: select the best assemblies
# ---------------------------------------------------------------------------

def get_nested(d, *keys, default=None):
    """Safely retrieve a nested dictionary value."""
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key, default)
        else:
            return default
    return d


def assembly_score(record):
    """
    Compute a quality score for an assembly. Higher is better.

    Priority (descending):
      1. Reference genome status (huge bonus)
      2. Has GFF3 annotation available (strong bonus — needed for gene overlap)
      3. Scaffold N50 (primary quality metric)
      4. Contig N50 (tiebreaker)
    """
    # Extract stats from the nested JSON structure
    scaffold_n50 = int(get_nested(record, "assembly_stats", "scaffold_n50", default=0) or 0)
    contig_n50 = int(get_nested(record, "assembly_stats", "contig_n50", default=0) or 0)
    has_annotation = 1 if get_nested(record, "annotation_info") else 0
    refseq_cat = get_nested(record, "assembly_info", "refseq_category", default="")
    is_refseq = 1 if refseq_cat == "reference genome" else 0

    return (is_refseq * 1e18) + (has_annotation * 1e15) + (scaffold_n50 * 1e6) + contig_n50


def rank_and_select(assemblies, max_n=MAX_GENOMES_PER_ORDER):
    """
    Rank assemblies by quality score and return the top N.
    Also deduplicates by species — only keep the best assembly per species
    so we get taxonomic breadth within the order.
    """
    # Sort by score descending
    ranked = sorted(assemblies, key=assembly_score, reverse=True)

    # Deduplicate by species: keep only the best assembly per species
    seen_species = set()
    selected = []
    for rec in ranked:
        species = get_nested(rec, "organism", "organism_name", default="unknown")
        if species in seen_species:
            continue
        seen_species.add(species)
        selected.append(rec)
        if len(selected) >= max_n:
            break

    return selected


# ---------------------------------------------------------------------------
# Download and organize
# ---------------------------------------------------------------------------

def download_assembly(order_name, record):
    """
    Download a single genome assembly (FASTA + GFF3) and organize files
    into genomes/<Order>/<Accession>/.

    Returns a metadata dict for the summary table, or None on failure.
    """
    env = get_env_with_bin()

    # Extract key identifiers
    accession = get_nested(record, "accession", default=None)
    if not accession:
        # Try alternate path in JSON structure
        accession = get_nested(record, "current_accession", default=None)
    if not accession:
        logger.warning(f"  Skipping record with no accession in {order_name}")
        return None

    species = get_nested(record, "organism", "organism_name", default="unknown")
    scaffold_n50 = int(get_nested(record, "assembly_stats", "scaffold_n50", default=0) or 0)
    contig_n50 = int(get_nested(record, "assembly_stats", "contig_n50", default=0) or 0)
    genome_size = int(get_nested(record, "assembly_stats", "total_sequence_length", default=0) or 0)
    has_annotation = bool(get_nested(record, "annotation_info"))
    refseq_cat = get_nested(record, "assembly_info", "refseq_category", default="na")
    asm_level = get_nested(record, "assembly_info", "assembly_level", default="unknown")

    # Create output directory
    order_dir = OUTPUT_DIR / order_name
    asm_dir = order_dir / accession
    asm_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded (resume support)
    fna_files = list(asm_dir.glob("*.fna")) + list(asm_dir.glob("*.fna.gz"))
    if fna_files:
        logger.info(f"    {accession} ({species}) — already downloaded, skipping")
        return {
            "order": order_name,
            "species": species,
            "accession": accession,
            "assembly_level": asm_level,
            "scaffold_n50": scaffold_n50,
            "contig_n50": contig_n50,
            "genome_size_mb": round(genome_size / 1e6, 1),
            "has_annotation": has_annotation,
            "refseq_category": refseq_cat,
            "status": "already_present",
        }

    # Download to a temporary zip file
    zip_path = order_dir / f"{accession}.zip"
    cmd = [
        "datasets", "download", "genome", "accession", accession,
        "--include", INCLUDE_DATA,
        "--filename", str(zip_path),
    ]
    logger.info(f"    Downloading {accession} ({species})...")
    logger.debug(f"    Command: {' '.join(cmd)}")

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                env=env, timeout=DOWNLOAD_TIMEOUT
            )
            if result.returncode == 0:
                break
            else:
                logger.warning(
                    f"    Download failed (attempt {attempt}): {result.stderr.strip()}"
                )
                if attempt == RETRY_ATTEMPTS:
                    logger.error(f"    FAILED to download {accession} after {RETRY_ATTEMPTS} attempts")
                    return {
                        "order": order_name, "species": species,
                        "accession": accession, "assembly_level": asm_level,
                        "scaffold_n50": scaffold_n50, "contig_n50": contig_n50,
                        "genome_size_mb": round(genome_size / 1e6, 1),
                        "has_annotation": has_annotation,
                        "refseq_category": refseq_cat, "status": "FAILED",
                    }
                time.sleep(2 ** attempt)
        except subprocess.TimeoutExpired:
            logger.warning(f"    Download timeout (attempt {attempt})")
            if attempt == RETRY_ATTEMPTS:
                return {
                    "order": order_name, "species": species,
                    "accession": accession, "assembly_level": asm_level,
                    "scaffold_n50": scaffold_n50, "contig_n50": contig_n50,
                    "genome_size_mb": round(genome_size / 1e6, 1),
                    "has_annotation": has_annotation,
                    "refseq_category": refseq_cat, "status": "TIMEOUT",
                }
            time.sleep(2 ** attempt)

    # Extract the zip and organize files
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(order_dir / "_tmp_extract")

        # The zip extracts to ncbi_dataset/data/<accession>/
        extracted_data = order_dir / "_tmp_extract" / "ncbi_dataset" / "data" / accession

        if extracted_data.exists():
            # Move genome FASTA files (.fna)
            for fna in extracted_data.glob("*.fna"):
                shutil.move(str(fna), str(asm_dir / fna.name))

            # Move GFF3 annotation files (.gff)
            for gff in extracted_data.glob("*.gff"):
                shutil.move(str(gff), str(asm_dir / gff.name))
            # Also check for genomic.gff in subdirectories
            for gff in extracted_data.rglob("*.gff"):
                if not (asm_dir / gff.name).exists():
                    shutil.move(str(gff), str(asm_dir / gff.name))

        # Clean up temp files
        shutil.rmtree(order_dir / "_tmp_extract", ignore_errors=True)
        zip_path.unlink(missing_ok=True)

        # Verify we got something
        final_fna = list(asm_dir.glob("*.fna"))
        final_gff = list(asm_dir.glob("*.gff"))
        actual_annotation = len(final_gff) > 0

        logger.info(
            f"    {accession}: genome={'YES' if final_fna else 'NO'}, "
            f"annotation={'YES' if actual_annotation else 'NO'}"
        )

        return {
            "order": order_name,
            "species": species,
            "accession": accession,
            "assembly_level": asm_level,
            "scaffold_n50": scaffold_n50,
            "contig_n50": contig_n50,
            "genome_size_mb": round(genome_size / 1e6, 1),
            "has_annotation": actual_annotation,
            "refseq_category": refseq_cat,
            "status": "downloaded",
        }

    except (zipfile.BadZipFile, OSError) as e:
        logger.error(f"    Failed to extract {accession}: {e}")
        shutil.rmtree(order_dir / "_tmp_extract", ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return {
            "order": order_name, "species": species,
            "accession": accession, "assembly_level": asm_level,
            "scaffold_n50": scaffold_n50, "contig_n50": contig_n50,
            "genome_size_mb": round(genome_size / 1e6, 1),
            "has_annotation": has_annotation,
            "refseq_category": refseq_cat, "status": "EXTRACT_FAILED",
        }


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def write_summary(records):
    """Write a TSV summary of all downloaded genomes."""
    summary_path = OUTPUT_DIR / "summary.tsv"
    columns = [
        "order", "species", "accession", "assembly_level",
        "scaffold_n50", "contig_n50", "genome_size_mb",
        "has_annotation", "refseq_category", "status",
    ]

    with open(summary_path, "w") as f:
        f.write("\t".join(columns) + "\n")
        for rec in records:
            row = [str(rec.get(col, "")) for col in columns]
            f.write("\t".join(row) + "\n")

    logger.info(f"Summary written to {summary_path} ({len(records)} assemblies)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """
    Main entry point. Surveys all insect orders for chromosome-level
    assemblies, selects the top 5 per order, downloads genomes + GFF3
    annotations, and writes a summary table.
    """
    setup_logging()
    logger.info("=" * 60)
    logger.info("Insect Genome Downloader for DR-scan")
    logger.info("=" * 60)

    # Step 0: Ensure NCBI datasets CLI is available
    ensure_datasets_cli()

    all_records = []        # Collect metadata for the summary table
    order_counts = {}       # Track genomes per order

    # Process each insect order
    for i, order in enumerate(INSECT_ORDERS, 1):
        logger.info(f"[{i}/{len(INSECT_ORDERS)}] Processing {order}...")

        # Survey available assemblies from NCBI
        assemblies = survey_order(order)

        if not assemblies:
            logger.info(f"  No chromosome-level assemblies found for {order}")
            order_counts[order] = 0
            continue

        # Rank and select the top assemblies
        selected = rank_and_select(assemblies)
        logger.info(
            f"  Selected {len(selected)} assemblies for download "
            f"(from {len(assemblies)} available)"
        )

        # Download each selected assembly
        for asm in selected:
            record = download_assembly(order, asm)
            if record:
                all_records.append(record)

        order_counts[order] = len(selected)

        # Brief pause between orders to be respectful to NCBI servers
        if i < len(INSECT_ORDERS):
            time.sleep(DELAY_BETWEEN_ORDERS)

    # Write the global summary table
    write_summary(all_records)

    # Print final summary to console
    logger.info("=" * 60)
    logger.info("DOWNLOAD COMPLETE")
    logger.info("=" * 60)
    total = sum(order_counts.values())
    orders_with_data = sum(1 for v in order_counts.values() if v > 0)
    logger.info(f"Total genomes downloaded: {total}")
    logger.info(f"Orders with data: {orders_with_data}/{len(INSECT_ORDERS)}")
    logger.info(f"Orders without chromosome-level assemblies: "
                f"{len(INSECT_ORDERS) - orders_with_data}")
    for order, count in sorted(order_counts.items()):
        if count > 0:
            logger.info(f"  {order}: {count} genome(s)")


if __name__ == "__main__":
    main()
