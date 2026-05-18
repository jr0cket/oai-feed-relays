#!/usr/bin/env python3
"""
Fetch AA district meeting pages, diff against snapshots, report changes.

Districts
---------
  Cornwall  D50  https://www.cornwallaa.ca/meetings          (Wix, JS-rendered)
  Renfrew   D70  https://aarenfrew.org/meetings-2/           (WordPress)
  Madawaska D78  http://aamadawaskavalley.org/meetings/      (WordPress + PDF)

Usage
-----
  python3 scripts/check_meetings.py [--dry-run]

Exit codes: 0 = no changes, 1 = changes written to snapshots/
"""

import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept-Encoding": "identity",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

CORNWALL_KEYWORDS = [
    "nooners", "seaway", "williamstown", "vankleek",
    "sunshine", "beginners", "serenity", "safe haven", "sunrays",
]


# ── HTTP ─────────────────────────────────────────────────────────────────────

def fetch(url: str, binary: bool = False, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            return data if binary else data.decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt == retries:
                raise
            time.sleep(15)


# ── HTML → text ───────────────────────────────────────────────────────────────

class _Extractor(HTMLParser):
    SKIP = frozenset(
        "script style noscript nav header footer aside form "
        "button select option iframe meta link".split()
    )
    BLOCK = frozenset(
        "p div li tr td th h1 h2 h3 h4 h5 h6 "
        "br hr dt dd section article main".split()
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, _):
        if tag in self.SKIP:
            self._depth += 1
        if tag in self.BLOCK and self._depth == 0:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._depth == 0:
            self._buf.append(data)

    def result(self) -> str:
        lines = [l.strip() for l in "".join(self._buf).splitlines()]
        out, prev_blank = [], False
        for l in lines:
            blank = not l
            if not (blank and prev_blank):
                out.append(l)
            prev_blank = blank
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    p = _Extractor()
    p.feed(html)
    return p.result()


# ── PDF ───────────────────────────────────────────────────────────────────────

def find_pdf_link(html: str, base: str) -> str | None:
    for m in re.finditer(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.I):
        href = m.group(1)
        return href if href.startswith("http") else urljoin(base, href)
    return None


def pdf_to_text(url: str) -> tuple[str, str]:
    """Return (filename, extracted_text)."""
    filename = urlparse(url).path.split("/")[-1]
    data = fetch(url, binary=True)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, text=True, check=True,
        )
        return filename, r.stdout
    finally:
        os.unlink(tmp)


# ── Diff ──────────────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    """Normalised non-comment content lines as a set for comparison."""
    out = set()
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        t = re.sub(r"\s+", " ", line).strip().lower()
        if t:
            out.add(t)
    return out


def make_diff(old: str, new: str) -> list[str]:
    old_t, new_t = _tokens(old), _tokens(new)
    removed = sorted(old_t - new_t)
    added = sorted(new_t - old_t)
    return [f"REMOVED: {t}" for t in removed] + [f"ADDED:   {t}" for t in added]


# ── Per-district fetch ────────────────────────────────────────────────────────

def check_cornwall() -> tuple[str, str | None]:
    """Returns (status, content). status: ok | wix-no-data | error:<msg>"""
    url = "https://www.cornwallaa.ca/meetings"
    try:
        html = fetch(url)
    except Exception as e:
        return f"error:{e}", None

    text = html_to_text(html)
    if any(kw in text.lower() for kw in CORNWALL_KEYWORDS):
        return "ok", text

    # Wix renders meetings via JS — look for any embedded JSON-LD with meeting data
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.I,
    ):
        if any(kw in block.lower() for kw in CORNWALL_KEYWORDS):
            return "ok", block

    return "wix-no-data", None


def check_renfrew() -> tuple[str, str | None]:
    url = "https://aarenfrew.org/meetings-2/"
    try:
        html = fetch(url)
    except Exception as e:
        return f"error:{e}", None
    return "ok", html_to_text(html)


def check_madawaska() -> tuple[str, str | None]:
    page_url = "http://aamadawaskavalley.org/meetings/"
    try:
        html = fetch(page_url)
    except Exception as e:
        return f"error:{e}", None

    pdf_url = find_pdf_link(html, page_url)
    if not pdf_url:
        return "error:no PDF link found on meetings page", None

    try:
        filename, text = pdf_to_text(pdf_url)
    except subprocess.CalledProcessError as e:
        return f"error:pdftotext failed — {e.stderr.strip()}", None
    except Exception as e:
        return f"error:{e}", None

    return "ok", f"# Source PDF: {filename}\n\n{text.strip()}"


# ── Main ──────────────────────────────────────────────────────────────────────

DISTRICTS = [
    {
        "key":     "cornwall-d50",
        "label":   "Cornwall (D50)",
        "title":   "Cornwall AA — District 50",
        "url":     "https://www.cornwallaa.ca/meetings",
        "snap":    SNAPSHOTS_DIR / "cornwall-d50.txt",
        "checker": check_cornwall,
    },
    {
        "key":     "renfrew-d70",
        "label":   "Renfrew (D70)",
        "title":   "AA Renfrew-Pontiac — District 70",
        "url":     "https://aarenfrew.org/meetings-2/",
        "snap":    SNAPSHOTS_DIR / "renfrew-d70.txt",
        "checker": check_renfrew,
    },
    {
        "key":     "madawaska-d78",
        "label":   "Madawaska (D78)",
        "title":   "Madawaska Valley AA — District 78",
        "url":     "http://aamadawaskavalley.org/meetings/",
        "snap":    SNAPSHOTS_DIR / "madawaska-d78.txt",
        "checker": check_madawaska,
    },
]


def build_snapshot(title: str, url: str, content: str) -> str:
    return (
        f"# SNAPSHOT: {title}\n"
        f"# Source URL: {url}\n"
        f"# Captured: {date.today().isoformat()}\n\n"
        f"{content.strip()}\n"
    )


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    SNAPSHOTS_DIR.mkdir(exist_ok=True)

    report: list[str] = []
    changed: list[str] = []
    errors: list[str] = []

    for d in DISTRICTS:
        status, content = d["checker"]()

        if status == "wix-no-data":
            msg = (
                f"{d['label']}: fetch failed — Wix renders meetings via JavaScript; "
                "plain HTML fetch returned no meeting data. Needs manual check or "
                "headless-browser fetch."
            )
            report.append(msg)
            errors.append(d["key"])
            continue

        if status.startswith("error:"):
            report.append(f"{d['label']}: fetch failed — {status[6:]}")
            errors.append(d["key"])
            continue

        snap_path: Path = d["snap"]
        old = snap_path.read_text("utf-8") if snap_path.exists() else ""
        new = build_snapshot(d["title"], d["url"], content)

        if not old:
            report.append(f"{d['label']}: initial snapshot captured")
            changed.append(d["key"])
            if not dry_run:
                snap_path.write_text(new, "utf-8")
            continue

        diffs = make_diff(old, new)
        if not diffs:
            report.append(f"{d['label']}: no changes")
        else:
            n = len(diffs)
            report.append(f"{d['label']}: {n} change{'s' if n != 1 else ''}")
            for line in diffs:
                report.append(f"  — {line}")
            changed.append(d["key"])
            if not dry_run:
                snap_path.write_text(new, "utf-8")

    output = "\n".join(report)
    print(output)

    # Write report file for the workflow's issue-creation step
    if not dry_run:
        (REPO_ROOT / "report.txt").write_text(output, "utf-8")

    gha_out = os.environ.get("GITHUB_OUTPUT", "")
    if gha_out:
        with open(gha_out, "a") as f:
            f.write(f"has_changes={'true' if changed else 'false'}\n")
            f.write(f"has_errors={'true' if errors else 'false'}\n")
            f.write(f"changed_keys={','.join(changed)}\n")

    gha_summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if gha_summary:
        with open(gha_summary, "w") as f:
            f.write(f"## AA District Snapshot Check — {date.today()}\n\n```\n{output}\n```\n")

    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
