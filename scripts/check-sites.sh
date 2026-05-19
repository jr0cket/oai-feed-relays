#!/usr/bin/env bash
# Fetch three AA district websites that have no TSML feed, compare to local
# snapshots, and write reports when content changes.
#
# Usage: bash scripts/check-sites.sh
# Deps:  curl, python3, pdftotext (poppler-utils)
set -euo pipefail

REPO_DIR="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
SNAPSHOTS="$REPO_DIR/snapshots"
REPORTS="$REPO_DIR/reports"
TODAY="$(date -u +%Y-%m-%d)"

declare -a CHANGED=()
declare -a FIRST_RUN=()

mkdir -p "$SNAPSHOTS" "$REPORTS"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

strip_html() {
  python3 -c "
import sys, re, html as H
t = sys.stdin.read()
t = re.sub(r'<script[^>]*>.*?</script>', '', t, flags=re.S)
t = re.sub(r'<style[^>]*>.*?</style>',  '', t, flags=re.S)
t = re.sub(r'<[^>]+>', ' ', t)
print(re.sub(r'\s+', ' ', H.unescape(t)).strip())
"
}

in_array() {
  local needle="$1"; shift
  local item
  for item in "$@"; do [[ "$item" == "$needle" ]] && return 0; done
  return 1
}

# fetch_and_check SLUG URL [pdf]
fetch_and_check() {
  local slug="$1"
  local url="$2"
  local mode="${3:-html}"
  local snapshot="$SNAPSHOTS/$slug.txt"

  echo "--- $slug ---"

  # ------------------------------------------------------------------
  # 1. Fetch content
  # ------------------------------------------------------------------
  local new_text=""

  if [[ "$mode" == "pdf" ]]; then
    # a) Get parent page and find the PDF href
    local page_html
    page_html="$(curl -sL --max-time 30 -A 'Mozilla/5.0 (compatible)' "$url")"
    local pdf_href
    pdf_href="$(echo "$page_html" \
      | grep -oP 'href="[^"]*\.pdf[^"]*"' \
      | head -1 \
      | sed 's/href="//;s/"//')" || true

    if [[ -z "$pdf_href" ]]; then
      echo "  WARNING: no PDF link found on $url — skipping" >&2
      return 0
    fi

    # Resolve relative URLs
    if [[ "$pdf_href" != http* ]]; then
      local base
      base="$(echo "$url" | grep -oP 'https?://[^/]+')"
      pdf_href="${base}${pdf_href}"
    fi
    echo "  PDF: $pdf_href"

    local tmp_pdf="/tmp/${slug}-check.pdf"
    curl -sL --max-time 60 -A 'Mozilla/5.0 (compatible)' "$pdf_href" -o "$tmp_pdf"
    new_text="$(pdftotext "$tmp_pdf" - \
      | python3 -c "import sys,re; t=sys.stdin.read(); print(re.sub(r'\s+', ' ', t).strip())")"

    # Save URL so the diff report can reference it
    echo "$pdf_href" > "/tmp/${slug}-pdf-url.txt"

  else
    new_text="$(curl -sL --max-time 30 -A 'Mozilla/5.0 (compatible)' "$url" | strip_html)"
  fi

  if [[ -z "$new_text" ]]; then
    echo "  WARNING: empty content fetched — skipping" >&2
    return 0
  fi

  # ------------------------------------------------------------------
  # 2. First run — write baseline, no change reported
  # ------------------------------------------------------------------
  if [[ ! -f "$snapshot" ]]; then
    printf '%s\n' "$new_text" > "$snapshot"
    FIRST_RUN+=("$slug")
    echo "  First run — baseline snapshot written ($(wc -c < "$snapshot") bytes)"
    return 0
  fi

  # ------------------------------------------------------------------
  # 3. Compare
  # ------------------------------------------------------------------
  printf '%s\n' "$new_text" > "/tmp/${slug}-new.txt"

  local diff_out
  diff_out="$(diff -u "$snapshot" "/tmp/${slug}-new.txt")" && {
    echo "  No changes"
    return 0
  }

  # ------------------------------------------------------------------
  # 4. Changed — update snapshot and write report
  # ------------------------------------------------------------------
  CHANGED+=("$slug")
  echo "  CHANGED"

  cp "/tmp/${slug}-new.txt" "$snapshot"

  local report="$REPORTS/$TODAY-$slug.md"
  {
    echo "# District site change: $slug — $TODAY"
    echo ""
    if [[ "$mode" == "pdf" ]]; then
      echo "**PDF:** $(cat "/tmp/${slug}-pdf-url.txt")"
      echo ""
    fi
    echo "## Diff (unified)"
    echo ""
    echo '```diff'
    echo "$diff_out"
    echo '```'
  } > "$report"
  echo "  Report written: reports/$TODAY-$slug.md"
}

# ---------------------------------------------------------------------------
# Run checks
# ---------------------------------------------------------------------------

fetch_and_check "cornwall"  "https://www.cornwallaa.ca/meetings"
fetch_and_check "renfrew"   "https://aarenfrew.org/meetings-2/"
fetch_and_check "madawaska" "http://aamadawaskavalley.org/meetings/" "pdf"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "=== Summary ($(date -u)) ==="
for slug in cornwall renfrew madawaska; do
  if in_array "$slug" "${FIRST_RUN[@]+"${FIRST_RUN[@]}"}"; then
    echo "  - $slug: first run — baseline written"
  elif in_array "$slug" "${CHANGED[@]+"${CHANGED[@]}"}"; then
    echo "  - $slug: CHANGED — see reports/$TODAY-$slug.md"
  else
    echo "  - $slug: no changes"
  fi
done

# ---------------------------------------------------------------------------
# GitHub Actions outputs
# ---------------------------------------------------------------------------

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  if [[ ${#CHANGED[@]} -gt 0 ]]; then
    changed_csv="$(IFS=,; echo "${CHANGED[*]}")"
    echo "changed_slugs=$changed_csv" >> "$GITHUB_OUTPUT"
    echo "has_changes=true"           >> "$GITHUB_OUTPUT"
  else
    echo "has_changes=false" >> "$GITHUB_OUTPUT"
  fi
  if [[ ${#FIRST_RUN[@]} -gt 0 ]]; then
    first_csv="$(IFS=,; echo "${FIRST_RUN[*]}")"
    echo "first_run_slugs=$first_csv" >> "$GITHUB_OUTPUT"
  fi
fi
