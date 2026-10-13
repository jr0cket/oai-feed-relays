#!/usr/bin/env python3
"""
Read the meeting lists on the three AA district websites that have no TSML
feed, compare them with the last snapshot, and report what changed.

Districts
---------
  Cornwall  D50  https://www.cornwallaa.ca/meetings          (Wix)
  Renfrew   D70  https://aarenfrew.org/meetings-2/           (WordPress)
  Madawaska D78  http://aamadawaskavalley.org/meetings/      (WordPress)

Every page is opened in headless Chromium (Playwright). Renfrew answers 406
to plain HTTP clients, and on the Cornwall page the "**CLOSED**" labels are
separate floating boxes that only line up with a meeting once the page is laid
out, so the reader works from rendered text and its position on the page.

Each snapshot holds one line per meeting:  DAY | meeting text [| NOTICE: ...]

A page that cannot be read, or that yields far fewer meetings than the last
snapshot, is reported as a READ PROBLEM. It never counts as meetings removed,
and its snapshot is left alone.

Usage
-----
  python3 scripts/check_meetings.py [--dry-run]

Exit code is always 0; results go to GITHUB_OUTPUT, report.txt and stdout.
"""
from __future__ import annotations

import difflib
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = REPO_ROOT / "snapshots"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
DAY_RE = re.compile(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
TIME_RE = re.compile(r"\b\d{1,2}(:\d\d)?\s*(am|pm|a\.m\.|p\.m\.)|\bnoon\b", re.I)
NOTICE_RE = re.compile(r"\*\*closed\*\*|meeting closed|cancel|temporarily|moved to", re.I)

# Text nodes with their position on the rendered page
JS_TEXT_NODES = r"""
() => {
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const p = n.parentElement;
    if (!p || p.closest('script,style,noscript,nav,header,footer')) continue;
    const t = n.textContent.replace(/[​ ]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    const r = document.createRange(); r.selectNodeContents(n);
    const b = r.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) continue;
    out.push({x: Math.round(b.left + scrollX), y: Math.round(b.top + scrollY), t});
  }
  return out;
}
"""


class ReadProblem(Exception):
    pass


def norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", " ", s).strip(" ,;")
    return re.sub(r"\s+([,.)])", r"\1", s)


# ── Browser ──────────────────────────────────────────────────────────────────

def open_pages(urls: list[str]) -> dict[str, dict]:
    """Return {url: {"nodes": [...], "text": innerText} or {"error": msg}}."""
    from playwright.sync_api import sync_playwright

    results: dict[str, dict] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            user_agent=UA, locale="en-CA", viewport={"width": 1280, "height": 1000},
            extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
        )
        for url in urls:
            last = "unknown"
            for attempt in range(3):
                page = ctx.new_page()
                try:
                    resp = page.goto(url, wait_until="load", timeout=60000)
                    status = resp.status if resp else 0
                    if status >= 400:
                        raise ReadProblem(f"HTTP {status}")
                    page.wait_for_timeout(3000)   # let Wix finish placing boxes
                    results[url] = {
                        "nodes": page.evaluate(JS_TEXT_NODES),
                        "text": page.evaluate("() => document.body.innerText"),
                    }
                    break
                except Exception as e:  # noqa: BLE001
                    last = str(e).splitlines()[0][:200]
                    page.wait_for_timeout(10000)
                finally:
                    page.close()
            else:
                results[url] = {"error": last}
        browser.close()
    return results


def text_lines(text: str) -> list[str]:
    lines = [norm(l.replace("​", "").replace("\xa0", " ")) for l in text.splitlines()]
    return [l for l in lines if l]


def between(lines: list[str], start: re.Pattern, stop: re.Pattern) -> list[str]:
    out, on = [], False
    for l in lines:
        if not on and start.search(l):
            on = True
        elif on and stop.search(l):
            break
        if on:
            out.append(l)
    return out


# ── Per-district readers ─────────────────────────────────────────────────────

def read_cornwall(page: dict) -> list[str]:
    """Two columns of day headings, each followed by meeting blocks. Notices
    such as **CLOSED** float over a block and are matched to it by position."""
    nodes = page["nodes"]
    heads = [n for n in nodes if n["t"].upper().rstrip(":") in DAYS]
    if len(heads) < 7:
        raise ReadProblem(f"found {len(heads)} of 7 day headings")
    cols = sorted({h["x"] for h in heads})
    col_x: list[int] = []
    for x in cols:                         # merge headings a few px apart
        if not col_x or x - col_x[-1] > 60:
            col_x.append(x)

    def column(x: int) -> int | None:
        best = None
        for i, cx in enumerate(col_x):
            if cx - 15 <= x:
                best = i
        if best is not None and x - col_x[best] > 290:
            return None
        return best

    notices, by_col = [], {i: [] for i in range(len(col_x))}
    for n in nodes:
        if n["t"] in ("Map",):
            continue
        if NOTICE_RE.search(n["t"]) and not n["t"].startswith("C (Closed)"):
            notices.append(n)
            continue
        c = column(n["x"])
        if c is not None:
            by_col[c].append(n)

    meetings = []   # dicts: day, col, y0, parts, notices
    for c, items in by_col.items():
        # merge nodes on the same row
        rows: list[list] = []
        for n in sorted(items, key=lambda n: (n["y"], n["x"])):
            if rows and abs(rows[-1][0] - n["y"]) <= 4:
                rows[-1][1].append(n["t"])
            else:
                rows.append([n["y"], [n["t"]]])
        day = None
        for y, parts in rows:
            line = norm(" ".join(parts))
            if line.upper().rstrip(":") in DAYS:
                day = line.upper().rstrip(":")
                continue
            if day is None:
                continue            # legend, prayer box, etc. above the first day
            if TIME_RE.search(line) or re.search(r"\(\d{1,2}(:\d\d)?\s*[ap]m\)", line, re.I):
                meetings.append({"day": day, "col": c, "y0": y,
                                 "parts": [line], "notices": []})
            elif meetings and meetings[-1]["col"] == c and meetings[-1]["day"] == day:
                if len(meetings[-1]["parts"]) < 6:
                    meetings[-1]["parts"].append(line)

    loose = []
    for n in notices:
        c = column(n["x"])
        # A label sitting on a meeting's title line belongs to that meeting
        # (30 px allows for Mac/Linux font differences). Wix moves other
        # labels around with the window width, so they stay page-level.
        near = [m for m in meetings if m["col"] == c and abs(m["y0"] - n["y"]) <= 30]
        if c is not None and near:
            min(near, key=lambda m: abs(m["y0"] - n["y"]))["notices"].append(norm(n["t"]))
        else:
            loose.append(norm(n["t"]))

    out = []
    for m in meetings:
        line = f"{m['day']} | " + ", ".join(m["parts"])
        if m["notices"]:
            line += " | NOTICE: " + "; ".join(sorted(m["notices"]))
        out.append(line)
    out += [f"PAGE NOTICE | {t}" for t in sorted(loose)]
    return out


def read_renfrew(page: dict) -> list[str]:
    lines = between(text_lines(page["text"]), re.compile(r"^Monday:?$", re.I),
                    re.compile(r"^GRAPEVINE$|^Last edited", re.I))
    out, day = [], None
    for l in lines:
        h = l.rstrip(":").upper()
        if h in DAYS or h == "MONTHLY":
            day = h
            continue
        if day == "MONTHLY" and out and out[-1].startswith("MONTHLY"):
            out[-1] += ", " + l       # the district meeting runs over several lines
        elif day:
            out.append(f"{day} | {l}")
    return out


def read_madawaska(page: dict) -> list[str]:
    lines = text_lines(page["text"])
    out = [f"PRINT LIST | {l.strip('=<> ')}" for l in lines
           if re.search(r"meeting list\s*-", l, re.I) and "Download" not in l]
    body = between(lines, re.compile(r"^Where & When", re.I),
                   re.compile(r"^\* OPEN MEETINGS", re.I))
    cur = None
    for l in body:
        if "Click here" in l or "Where & When" in l or "Meeting List" in l:
            continue
        if DAY_RE.match(l):
            if cur:
                out.append(cur)
            day, rest = l.split(" ", 1)
            cur = f"{day.upper()} | {rest}"
        elif cur:
            cur += ", " + re.sub(r"^(->|-)\s*", "", l)
    if cur:
        out.append(cur)
    return out


DISTRICTS = [
    {"key": "cornwall-d50", "label": "Cornwall (D50)", "title": "Cornwall A.A. - District 50",
     "url": "https://www.cornwallaa.ca/meetings", "reader": read_cornwall, "min": 10},
    {"key": "renfrew-d70", "label": "Renfrew (D70)", "title": "A.A. Renfrew-Pontiac - District 70",
     "url": "https://aarenfrew.org/meetings-2/", "reader": read_renfrew, "min": 10},
    {"key": "madawaska-d78", "label": "Madawaska (D78)", "title": "Madawaska Valley A.A. - District 78",
     "url": "http://aamadawaskavalley.org/meetings/", "reader": read_madawaska, "min": 4},
]


# ── Snapshots and diff ───────────────────────────────────────────────────────

def snapshot_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.strip() and not l.startswith("#")]


def build_snapshot(d: dict, lines: list[str]) -> str:
    head = (f"# SNAPSHOT: {d['title']}\n# Source URL: {d['url']}\n"
            f"# Captured: {date.today().isoformat()} (headless Chromium)\n"
            f"# One line per meeting: DAY | meeting [| NOTICE: ...]\n\n")
    return head + "\n".join(lines) + "\n"


def make_diff(old: list[str], new: list[str]) -> list[str]:
    key = lambda s: s.lower()
    oc, nc = Counter(map(key, old)), Counter(map(key, new))
    removed = [l for l in old if (oc - nc)[key(l)] > 0]
    added = [l for l in new if (nc - oc)[key(l)] > 0]
    out = []
    for r in list(removed):           # pair up edits to the same meeting
        same_day = [a for a in added if a.split(" | ")[0] == r.split(" | ")[0]]
        best = max(same_day, key=lambda a: difflib.SequenceMatcher(None, r, a).ratio(), default=None)
        if best and difflib.SequenceMatcher(None, r, best).ratio() >= 0.6:
            out += [f"CHANGED: {r}", f"     NOW: {best}"]
            removed.remove(r)
            added.remove(best)
    out += [f"REMOVED: {r}" for r in removed]
    out += [f"ADDED:   {a}" for a in added]
    return out


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    dry_run = "--dry-run" in sys.argv
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    report, changed, errors = [], [], []

    try:
        pages = open_pages([d["url"] for d in DISTRICTS])
    except Exception as e:  # noqa: BLE001
        pages = {d["url"]: {"error": "browser failed: " + str(e).splitlines()[0][:200]} for d in DISTRICTS}

    for d in DISTRICTS:
        snap = SNAPSHOTS_DIR / f"{d['key']}.txt"
        old = snapshot_lines(snap.read_text("utf-8")) if snap.exists() else []
        page = pages[d["url"]]
        try:
            if "error" in page:
                raise ReadProblem(page["error"])
            new = d["reader"](page)
            floor = max(d["min"], int(len(old) * 0.6))
            if len(new) < floor:
                raise ReadProblem(f"read only {len(new)} lines (expected at least {floor}); "
                                  "the page may be down or its layout changed")
        except ReadProblem as e:
            report.append(f"{d['label']}: READ PROBLEM - {e}. Nothing compared; snapshot kept. {d['url']}")
            errors.append(d["key"])
            continue
        except Exception as e:  # noqa: BLE001
            report.append(f"{d['label']}: READ PROBLEM - reader crashed ({e}). Nothing compared; snapshot kept.")
            errors.append(d["key"])
            continue

        diffs = make_diff(old, new) if old else []
        count = sum(1 for l in new if not l.startswith(("PAGE NOTICE", "PRINT LIST")))
        if not old:
            report.append(f"{d['label']}: first snapshot, {count} meetings")
        elif not diffs:
            report.append(f"{d['label']}: no changes ({count} meetings)")
        else:
            n = sum(1 for l in diffs if not l.lstrip().startswith("NOW:"))
            report.append(f"{d['label']}: {n} change{'s' if n != 1 else ''} ({count} meetings now) {d['url']}")
            report += [f"  {l}" for l in diffs]
        if diffs or not old:
            changed.append(d["key"])
            if not dry_run:
                snap.write_text(build_snapshot(d, new), "utf-8")
        if "--show" in sys.argv:
            report += [f"    {l}" for l in new]

    output = "\n".join(report)
    print(output)
    (REPO_ROOT / "report.txt").write_text(output + "\n", "utf-8")

    gha_out = os.environ.get("GITHUB_OUTPUT", "")
    if gha_out:
        with open(gha_out, "a") as f:
            f.write(f"has_changes={'true' if changed else 'false'}\n")
            f.write(f"has_errors={'true' if errors else 'false'}\n")
            f.write(f"changed_keys={','.join(changed)}\n")
            f.write(f"error_keys={','.join(errors)}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## District meeting pages, {date.today()}\n\n```\n{output}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
