#!/usr/bin/env python3
"""
Build data/suburb-medians.json from the Victorian Property Sales Report.

The Valuer-General publishes median sale prices by suburb each quarter as three
separate XLS files (houses, units, vacant land). This finds the newest release of
each via the DataVic CKAN API, parses them, and merges them into one JSON file
the static site can fetch.

Two things this has to work around:
  * land.vic.gov.au sits behind Cloudflare and returns a 403 challenge page to
    anything without a browser User-Agent. The download still succeeds with one.
  * A "^" marker in the column beside a median means the Valuer-General considers
    that figure unreliable (too few sales). Those are kept but flagged.

Suburb medians are the finest-grained price data published for free. There is no
free source of per-address sale prices or valuations.

Usage:  python3 build-medians.py [-o data/suburb-medians.json]
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

import xlrd

CKAN = "https://discover.data.vic.gov.au/api/3/action/package_show?id="
WARMUP = "https://www.land.vic.gov.au/valuations/resources-and-reports/property-sales-statistics"
DATASETS = {
    "house": "victorian-property-sales-report-median-house-by-suburb",
    "unit":  "victorian-property-sales-report-median-unit-by-suburb",
    "land":  "victorian-property-sales-report-median-vacant-land-by-suburb",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

# Set by warm_up(); shared by every download so the clearance cookie is reused.
_COOKIE_JAR: str | None = None


def _curl(url: str, out: str, timeout: int = 120, referer: str | None = None) -> int:
    """Download via curl. Python's TLS fingerprint is rejected by Cloudflare here,
    so urllib/requests get a 403 even holding a valid clearance cookie."""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-A", UA,
           "-H", "Accept-Language: en-AU,en;q=0.9",
           "-b", _COOKIE_JAR, "-c", _COOKIE_JAR,
           "-o", out, "-w", "%{http_code}", url]
    if referer:
        cmd[-1:-1] = ["-H", f"Referer: {referer}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"curl failed: {res.stderr.strip()}")
    return int(res.stdout.strip() or 0)


def warm_up() -> None:
    """land.vic.gov.au sits behind Cloudflare. Hitting the landing page first
    yields a __cf_bm cookie; the challenge page itself returns 403, which is
    expected and fine — only the cookie matters."""
    global _COOKIE_JAR
    _COOKIE_JAR = os.path.join(tempfile.mkdtemp(prefix="vpsr-"), "cookies.txt")
    open(_COOKIE_JAR, "w").close()
    code = _curl(WARMUP, os.devnull, timeout=60)
    print(f"warm-up {code} (403 here is normal — the cookie is what counts)",
          file=sys.stderr)
    time.sleep(2)


def fetch(url: str, timeout: int = 120) -> bytes:
    """Returns the file bytes, or raises. 404 fails fast (DTP's CKAN carries dead
    links); 403 is retried, since that's the Cloudflare challenge."""
    tmp = os.path.join(os.path.dirname(_COOKIE_JAR), "download.bin")
    code = 0
    for attempt in (1, 2, 3):
        code = _curl(url, tmp, timeout=timeout, referer=WARMUP)
        blob = pathlib.Path(tmp).read_bytes() if os.path.exists(tmp) else b""
        if code == 200 and not blob[:15].lstrip().lower().startswith(b"<!doctype html"):
            return blob
        if code == 404:
            raise FileNotFoundError(f"HTTP 404 — dead link in CKAN: {url}")
        if attempt < 3:
            print(f"  retry {attempt} (HTTP {code})", file=sys.stderr)
            time.sleep(4 * attempt)
            warm_up()
    raise RuntimeError(f"blocked after 3 attempts (HTTP {code}): {url}")


def resources_newest_first(dataset_id: str) -> list[tuple[str, str]]:
    """All dated resources as (url, label), newest quarter first."""
    with urllib.request.urlopen(CKAN + dataset_id, timeout=60) as r:
        pkg = json.load(r)
    if not pkg.get("success"):
        raise RuntimeError(f"CKAN lookup failed for {dataset_id}")

    found = []
    for res in pkg["result"]["resources"]:
        name = res.get("name") or ""
        m = re.search(r"(January|February|March|April|May|June|July|August|"
                      r"September|October|November|December)\s+(\d{4})", name)
        url = res.get("url") or ""
        # Internet Archive entries point at truncated paths and never resolve.
        if not m or "web.archive.org" in url:
            continue
        found.append(((int(m.group(2)), MONTHS[m.group(1)]), url, name.strip()))

    if not found:
        raise RuntimeError(f"no dated resource found in {dataset_id}")
    found.sort(key=lambda t: t[0], reverse=True)
    return [(url, label) for _, url, label in found]


def newest_working(dataset_id: str) -> tuple[bytes, str, str]:
    """Walk back from the newest quarter until one actually downloads."""
    last = None
    for url, label in resources_newest_first(dataset_id)[:6]:
        try:
            return fetch(url), url, label
        except FileNotFoundError as exc:
            print(f"  skipping {label}: {exc}", file=sys.stderr)
            last = exc
    raise RuntimeError(f"no downloadable resource in {dataset_id} ({last})")


PERIOD_RE = re.compile(r"^([A-Z][a-z]{2})\s*-\s*([A-Z][a-z]{2})$")
YEAR_RE = re.compile(r"^(\d{4})(?:\.0)?$")


def quarter_labels(sheet) -> dict[int, str]:
    """Median values sit in columns 1,3,5,7,9. The house and unit workbooks put
    the month range and year on *different* header rows ("Locality" shifts
    everything down by one), and format the range differently ("Jul - Sep" vs
    "Oct-Dec"), so scan the top rows rather than assuming fixed positions."""
    labels = {}
    for col in range(1, 10, 2):
        period = year = None
        for row in range(min(5, sheet.nrows)):
            cell = str(sheet.cell_value(row, col)).strip()
            if period is None:
                m = PERIOD_RE.match(cell)
                if m:
                    period = f"{m.group(1)}-{m.group(2)}"
                    continue
            if year is None:
                m = YEAR_RE.match(cell)
                if m:
                    year = m.group(1)
        if period and year:
            labels[col] = f"{period} {year}"
    return labels


def parse(blob: bytes) -> tuple[dict[str, dict], list[str]]:
    """-> ({suburb: {quarter: {'value': int, 'unreliable': bool}}}, ordered quarters)"""
    sheet = xlrd.open_workbook(file_contents=blob).sheet_by_index(0)
    labels = quarter_labels(sheet)
    out: dict[str, dict] = {}

    for row in range(5, sheet.nrows):
        suburb = str(sheet.cell_value(row, 0)).strip().upper()
        # Trailing rows hold totals and footnotes, not suburbs.
        if not suburb or suburb.startswith(("TOTAL", "NOTE", "*", "^")):
            continue

        per_quarter = {}
        for col, label in labels.items():
            raw = sheet.cell_value(row, col)
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            flag = str(sheet.cell_value(row, col + 1)).strip() if col + 1 < sheet.ncols else ""
            per_quarter[label] = {"value": value, "unreliable": flag == "^"}

        counts = {}
        for key, col in (("quarterSales", 11), ("annualSales", 12)):
            if col < sheet.ncols:
                try:
                    counts[key] = int(float(sheet.cell_value(row, col)))
                except (TypeError, ValueError):
                    pass

        if per_quarter:
            out[suburb] = {"quarters": per_quarter, **counts}

    return out, list(labels.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="data/suburb-medians.json")
    args = ap.parse_args()

    warm_up()

    parsed, quarters, sources = {}, [], {}
    for kind, dataset_id in DATASETS.items():
        print(f"{kind}:", file=sys.stderr)
        blob, url, label = newest_working(dataset_id)
        print(f"  {label}\n  {url}", file=sys.stderr)
        data, qs = parse(blob)
        parsed[kind] = data
        sources[kind] = {"release": label, "url": url}
        # Houses are the most complete series, so they set the quarter order.
        if kind == "house" or not quarters:
            quarters = qs
        print(f"  {len(data)} suburbs", file=sys.stderr)

    suburbs = {}
    for name in sorted(set().union(*(d.keys() for d in parsed.values()))):
        rows = []
        for q in quarters:
            house = parsed["house"].get(name, {}).get("quarters", {}).get(q)
            unit = parsed["unit"].get(name, {}).get("quarters", {}).get(q)
            land = parsed["land"].get(name, {}).get("quarters", {}).get(q)
            if not (house or unit or land):
                continue
            rows.append({
                "quarter": q,
                "house": house["value"] if house else None,
                "unit": unit["value"] if unit else None,
                "land": land["value"] if land else None,
                "unreliable": bool((house or {}).get("unreliable")
                                   or (unit or {}).get("unreliable")
                                   or (land or {}).get("unreliable")),
            })
        if rows:
            h = parsed["house"].get(name, {})
            suburbs[name] = {
                "quarters": rows,
                # Both counts describe the release quarter only, not each row,
                # so they live here rather than being repeated per quarter.
                "latestQuarterHouseSales": h.get("quarterSales"),
                "annualHouseSales": h.get("annualSales"),
            }

    payload = {
        "released": sources["house"]["release"],
        "generated": dt.date.today().isoformat(),
        "source": "Victorian Property Sales Report, Valuer-General Victoria (CC BY 4.0)",
        "sources": sources,
        "quarters": quarters,
        "suburbs": suburbs,
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")))
    print(f"\nwrote {out} — {len(suburbs)} suburbs, quarters: {', '.join(quarters)}",
          file=sys.stderr)

    if len(suburbs) < 300:
        print("WARNING: suspiciously few suburbs; the file layout may have changed.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
