#!/usr/bin/env python3
"""Suggest a `careers:` block for portfolio companies.

Looks at each company's homepage and common careers paths for links to a known
applicant tracking system, then prints what it found. It does not edit
portfolio.yaml; copy the suggestion in after checking it.

Usage:  python scripts/detect_careers.py                 # companies with type: none
        python scripts/detect_careers.py --all
        python scripts/detect_careers.py --name "Atom"
        python scripts/detect_careers.py --url https://newco.ai   # a company not in the file yet
"""
import argparse
import re
import sys
from urllib.parse import urljoin

import yaml
from fetch_jobs import PORTFOLIO, SESSION, TIMEOUT

PATHS = ["", "careers", "careers/", "jobs", "join-us", "company", "about"]

FINGERPRINTS = [  # (type, regex with named groups) — order matters
    ("comeet", r"comeet\.com?/jobs/(?P<slug>[a-z0-9-]+)/(?P<uid>[0-9A-F]{2}\.[0-9A-F]{3})"),
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<slug>[a-z0-9-]+)"),
    ("lever", r"jobs\.lever\.co/(?P<slug>[a-z0-9-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9.-]+)"),
    ("workable", r"apply\.workable\.com/(?P<slug>[a-z0-9-]+)"),
    ("hibob", r"(?P<slug>[a-z0-9-]+)\.careers\.hibob\.com"),
    ("breezy (not supported, use html)", r"(?P<slug>[a-z0-9-]+)\.breezy\.hr"),
    ("bamboohr (not supported, use html)", r"(?P<slug>[a-z0-9-]+)\.bamboohr\.com"),
]
JOB_LINK = re.compile(r'href="([^"]*/(?:careers|jobs?|positions?|openings)/[a-z0-9][a-z0-9-]{3,}[^"]*)"', re.I)


def scan(website):
    found, job_links, careers_pages = {}, set(), []
    for p in PATHS:
        url = urljoin(website if website.endswith("/") else website + "/", p)
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        html = r.text
        for t, rx in FINGERPRINTS:
            for m in re.finditer(rx, html):
                found.setdefault(t, m.groupdict())
        links = [urljoin(r.url, h) for h in JOB_LINK.findall(html)]
        if p and links:
            careers_pages.append(r.url)
            job_links.update(links)
    return found, careers_pages, sorted(job_links)[:5]


def suggest(found, careers_pages, job_links):
    for t, g in found.items():
        if "not supported" in t:
            continue
        extra = f", uid: \"{g['uid']}\"" if "uid" in g else ""
        return f"careers: {{type: {t}, slug: {g['slug']}{extra}}}"
    if careers_pages and job_links:
        path = re.sub(r"^https?://[^/]+", "", job_links[0]).split("?")[0].rsplit("/", 1)[0]
        return f'careers: {{type: html, url: "{careers_pages[0]}", link: "{path}/[a-z0-9-]+"}}  # check titles with --only'
    return "careers: {type: none}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--name")
    ap.add_argument("--url")
    a = ap.parse_args()
    if a.url:
        targets = [{"name": a.url, "website": a.url}]
    else:
        cos = yaml.safe_load(PORTFOLIO.read_text())["companies"]
        targets = [c for c in cos if (a.name and c["name"].lower() == a.name.lower())
                   or (not a.name and (a.all or (c.get("careers") or {}).get("type") == "none"))]
    if not targets:
        sys.exit("No matching companies.")
    for c in targets:
        found, pages, links = scan(c["website"])
        print(f"\n{c['name']}  ({c['website']})")
        for t, g in found.items():
            print(f"  found {t}: {g}")
        if pages:
            print(f"  careers page: {pages[0]}  e.g. {links[0] if links else ''}")
        print("  suggest ->", suggest(found, pages, links))


if __name__ == "__main__":
    main()
