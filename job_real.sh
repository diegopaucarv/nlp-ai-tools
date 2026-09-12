#!/bin/bash

# Configuration
OUTPUT_DIR="out"
CHUNK_SIZE=5
FINAL_MD="full_book.md"

# Check if PDF file argument is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <path_to_pdf> [--from <start_page>] [--to <end_page>]"
    exit 1
fi

INPUT_PDF="$1"
shift # Shift to move past the filename to the remaining flags

# Initialize default variables
FROM_PAGE=1
TO_PAGE=""

# Parse command line arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --from) FROM_PAGE="$2"; shift ;;
        --to) TO_PAGE="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# Check if input file exists
if [ ! -f "$INPUT_PDF" ]; then
    echo "Error: File '$INPUT_PDF' not found."
    exit 1
fi

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Get total number of pages using pdfinfo
TOTAL_PAGES=$(pdfinfo "$INPUT_PDF" | awk '/^Pages:/ {print $2}')

if [ -z "$TOTAL_PAGES" ]; then
    echo "Error: Could not read page count. Check if the file is a valid PDF."
    exit 1
fi

# Sanitize 'FROM' page
if [ "$FROM_PAGE" -lt 1 ]; then
    FROM_PAGE=1
fi

# Set 'TO' page default or sanitize if it exceeds total pages
if [ -z "$TO_PAGE" ] || [ "$TO_PAGE" -gt "$TOTAL_PAGES" ]; then
    TO_PAGE=$TOTAL_PAGES
fi

# Validate logic
if [ "$FROM_PAGE" -gt "$TO_PAGE" ]; then
    echo "Error: --from page ($FROM_PAGE) cannot be greater than --to page ($TO_PAGE)."
    exit 1
fi

echo "Total PDF pages: $TOTAL_PAGES"
echo "Processing from page $FROM_PAGE to $TO_PAGE"

# Loop through the document in chunks
for (( START=FROM_PAGE; START<=TO_PAGE; START+=CHUNK_SIZE )); do
    END=$((START + CHUNK_SIZE - 1))

    if [ "$END" -gt "$TO_PAGE" ]; then
        END=$TO_PAGE
    fi

    echo "================================================="
    echo "Processing pages $START to $END..."
    echo "================================================="

    TEMP_PDF="temp_chunk_pages_${START}_to_${END}.pdf"
    qpdf --empty --pages "$INPUT_PDF" "$START-$END" -- "$TEMP_PDF"

    marker_single "$TEMP_PDF" --output_dir "$OUTPUT_DIR" --output_format markdown --strip_existing_ocr

    rm "$TEMP_PDF"

    echo "Forcing RAM cleanup..."
    sync
    sudo sysctl -w vm.drop_caches=3 > /dev/null

    sleep 2
done

echo "================================================="
echo "Batch processing complete! Merging files..."
echo "================================================="

find "$OUTPUT_DIR" -name "*.md" | sort -V | xargs cat > "$FINAL_MD"

echo "Done. Your combined file is saved as $FINAL_MD"
