#!/usr/bin/env python3
"""Pull open roles from every portfolio company and write docs/jobs.json.

Usage:  python scripts/fetch_jobs.py            # fetch everything
        python scripts/fetch_jobs.py --only Matia  # one company, prints its jobs

A company whose source fails keeps yesterday's jobs (marked stale) so a flaky
site never empties the board. Failures are listed in the run summary.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORTFOLIO = ROOT / "portfolio.yaml"
OUT = ROOT / "docs" / "jobs.json"
TIMEOUT = 25

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PortfolioJobBoard/1.0",
    "Accept-Language": "en-US,en;q=0.9",
})


def get_json(url, **kw):
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def get_text(url):
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def day(s):
    """First 10 chars of an ISO timestamp, or ''."""
    return (s or "")[:10] if isinstance(s, str) else ""


# --------------------------------------------------------------------------
# Source adapters. Each returns a list of dicts with any of:
#   title, location, department, url, workplace (Remote/Hybrid/On-site), posted, card
# --------------------------------------------------------------------------

def src_greenhouse(c):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true")
    return [{
        "title": j["title"],
        "location": (j.get("location") or {}).get("name", ""),
        "department": ((j.get("departments") or [{}])[0] or {}).get("name", ""),
        "url": j["absolute_url"],
        "posted": day(j.get("first_published") or j.get("updated_at")),
    } for j in data.get("jobs", [])]


def src_lever(c):
    data = get_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        out.append({
            "title": j["text"],
            "location": cat.get("location", ""),
            "department": cat.get("department") or cat.get("team") or "",
            "url": j["hostedUrl"],
            "workplace": {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(j.get("workplaceType", ""), ""),
            "posted": dt.datetime.utcfromtimestamp(ts / 1000).date().isoformat() if ts else "",
        })
    return out


def src_ashby(c):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    return [{
        "title": j["title"],
        "location": j.get("location", ""),
        "department": j.get("department") or j.get("team") or "",
        "url": j["jobUrl"],
        # Ashby's isRemote flag is set on hybrid roles too; workplaceType is reliable.
        "workplace": {"Remote": "Remote", "Hybrid": "Hybrid", "OnSite": "On-site"}.get(j.get("workplaceType") or "", ""),
        "posted": day(j.get("publishedAt")),
    } for j in data.get("jobs", []) if j.get("isListed", True)]


def src_workable(c):
    slug, out, token = c["slug"], [], None
    while True:
        body = {"token": token} if token else {}
        r = SESSION.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", json=body, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for j in data.get("results", []):
            loc = j.get("location") or {}
            out.append({
                "title": j["title"],
                "location": ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                "department": (j.get("department") or [""])[0] if isinstance(j.get("department"), list) else j.get("department") or "",
                "url": f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
                "workplace": "Remote" if j.get("remote") else "",
                "posted": day(j.get("published")),
            })
        token = data.get("nextPage")
        if not token:
            return out


def src_comeet(c):
    # The public Comeet jobs page embeds the full list as JSON; no API token needed.
    page = get_text(f"https://www.comeet.com/jobs/{c['slug']}/{c['uid']}")
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(?=\[)", page)
    if not m:
        raise ValueError("Comeet page has no COMPANY_POSITIONS_DATA; check slug/uid")
    data, _ = json.JSONDecoder().raw_decode(page, m.end())
    return [{
        "title": j["name"],
        "location": (j.get("location") or {}).get("name", ""),
        "department": j.get("department") or "",
        # The hosted page always works; some companies' own "active page" links are stale.
        "url": j.get("url_comeet_hosted_page") or j.get("url_active_page"),
        "workplace": {"Remote": "Remote", "Hybrid": "Hybrid", "On-site": "On-site", "Onsite": "On-site"}.get(j.get("workplace_type") or "", ""),
        "posted": day(j.get("time_updated")),
    } for j in data if not j.get("is_internal")]


def src_hibob(c):
    slug = c["slug"]
    data = get_json(f"https://{slug}.careers.hibob.com/api/job-ad", headers={"companyIdentifier": slug})
    return [{
        "title": j["title"],
        "location": j.get("site") or j.get("country") or "",
        "department": j.get("department") or "",
        "url": f"https://{slug}.careers.hibob.com/jobs/{j['id']}",
        "workplace": j.get("workspaceType") or "",
        "posted": day(j.get("publishedAt")),
    } for j in data.get("jobAdDetails", [])]


HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
JUNK_TITLES = re.compile(r"^(read more|apply( now)?|view details|learn more|see more|details)$", re.I)


def _text(el):
    """Visible text with spaces between blocks (good for scanning a whole card)."""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _title_text(el):
    """Text exactly as written. Some sites (Framer) wrap every letter in its own <span>
    for animation; joining with "" keeps "Data Science" instead of "D a t a S c i e n c e"."""
    return re.sub(r"\s+", " ", el.get_text("")).strip() if el else ""


def _is_title_el(el):
    cls = " ".join(el.get("class") or []).lower()
    return el.name in HEADINGS or "title" in cls


def _guess_title(card, anchor):
    inside = [e for e in anchor.find_all(True) if _is_title_el(e)]
    if inside:
        return _title_text(inside[0])
    last = None
    for el in card.find_all(True):
        if el is anchor:
            break
        if _is_title_el(el) and _title_text(el):
            last = el
    if last is not None:
        return _title_text(last)
    # No heading anywhere: use the first line of text inside the link (cards that are one big <a>).
    for s in anchor.stripped_strings:
        s = re.sub(r"\s+", " ", s)
        if s and not JUNK_TITLES.match(s):
            return s
    return ""


def src_html(c):
    """Scrape a company's own careers page (Webflow, Framer, WordPress...)."""
    base = c["url"]
    soup = BeautifulSoup(get_text(base), "html.parser")
    sel = lambda card, key: _title_text(card.select_one(c[key])) if c.get(key) else ""
    out = []

    if c.get("item"):  # one card per job, selected directly
        for card in soup.select(c["item"]):
            a = card.select_one(c.get("link_selector", "a[href]"))
            out.append({
                "title": sel(card, "title") or _title_text(card.find(HEADINGS)),
                "location": sel(card, "location"),
                "department": sel(card, "department"),
                "url": urljoin(base, a["href"]) if a and a.has_attr("href") else c.get("apply_url", base),
                "card": _text(card),
            })
        return [j for j in out if j["title"]]

    pat = re.compile(c["link"])
    seen = set()
    for a in soup.find_all("a", href=True):
        if not pat.search(a["href"]):
            continue
        href = urljoin(base, a["href"])
        if href in seen or href.rstrip("/") == base.rstrip("/"):
            continue
        seen.add(href)
        # Climb to the largest ancestor that still holds only this one job link.
        card = a
        while card.parent is not None and card.parent.name != "[document]":
            links = {urljoin(base, x["href"]) for x in card.parent.find_all("a", href=True) if pat.search(x["href"])}
            if len(links) > 1:
                break
            card = card.parent
        title = sel(card, "title") or _guess_title(card, a)
        if title:
            out.append({
                "title": title,
                "location": sel(card, "location"),
                "department": sel(card, "department"),
                "url": href,
                "card": _text(card)[:600],
            })
    return out


SOURCES = {
    "greenhouse": src_greenhouse, "lever": src_lever, "ashby": src_ashby,
    "workable": src_workable, "comeet": src_comeet, "hibob": src_hibob, "html": src_html,
}


def careers_url(company):
    c = company.get("careers") or {}
    t = c.get("type", "none")
    return {
        "greenhouse": lambda: f"https://job-boards.greenhouse.io/{c['slug']}",
        "lever": lambda: f"https://jobs.lever.co/{c['slug']}",
        "ashby": lambda: f"https://jobs.ashbyhq.com/{c['slug']}",
        "workable": lambda: f"https://apply.workable.com/{c['slug']}/",
        "comeet": lambda: f"https://www.comeet.com/jobs/{c['slug']}/{c['uid']}",
        "hibob": lambda: f"https://{c['slug']}.careers.hibob.com/jobs",
        "html": lambda: c["url"],
    }.get(t, lambda: c.get("careers_url") or company["website"])()


# --------------------------------------------------------------------------
# Normalization: one shape, standard team names, regions, remote flag
# --------------------------------------------------------------------------

FUNCTIONS = [  # first match wins; checked against the title, then the department
    ("Finance & Legal", r"financ|accountant|accounts? (receivable|payable)|controller|treasury|legal|counsel|payroll|\bcfo\b"),
    ("People", r"recruit|talent|\bpeople\b|\bhr\b|human resources"),
    ("Data & AI", r"data scien|data analy|analytics|data lead|data engineer|machine learning|\bml\b|research|prompt engineer"),
    ("Design", r"design|\bux\b|\bui\b"),
    ("Customer Success", r"customer success|customer support|support (agent|specialist|engineer)|onboarding|implementation|\bcx\b|client success"),
    ("Sales & BD", r"sales|account executive|account manager|business development|\bsdr\b|\bbdr\b|partnership|solutions engineer|\bgtm\b"),
    ("Marketing", r"marketing|growth|performance|user acquisition|content|brand|\bseo\b|communications|events"),
    ("Product", r"product|game economy|game design"),
    ("Operations", r"operation|office|workplace|project|program|property|coordinator|assistant|revenue op|strategist|\bops\b"),
    ("Engineering", r"engineer|developer|architect|devops|\bsre\b|tech lead|r&d|\brnd\b|\bqa\b|\bit\b|security|platform|software|backend|frontend|full ?stack|cobol|unity|integrations"),
]

REGIONS = [
    ("Israel", r"tel[- ]?aviv|israel|giv'?atayim|herzliya|haifa|jerusalem|ramat gan|petah|ra'?anana|yafo|be'?er sheva|\bil\b"),
    ("United States", r"\bus\b|\busa\b|united states|new york|\bnyc\b|austin|\btx\b|san francisco|\bsf\b|boston|chicago|mid ?west|\best\b|\bpst\b|seattle|los angeles|miami|westchester|north america"),
    ("Europe", r"london|\buk\b|united kingdom|poland|berlin|paris|europe|\bemea\b|amsterdam|madrid|lisbon|warsaw|krakow"),
    ("Asia-Pacific", r"beijing|seoul|china|korea|singapore|tokyo|japan|india|australia|\bapac\b"),
]

LOCATION_CLEANUP = [
    (r"\bTel[- ]Aviv(-Yafo)?\b", "Tel Aviv"),
    (r"\s*\((Onsite|On-site|Hybrid)\)", ""),
    (r",?\s*(Full[- ]time|Part[- ]time|Contract|Internship)\b", ""),
    (r"\s+", " "),
]


def classify(title, department):
    for text in (title, department):
        low = (text or "").lower()
        for name, rx in FUNCTIONS:
            if re.search(rx, low):
                return name
    return "Other"


def region_of(loc):
    for name, rx in REGIONS:
        if re.search(rx, loc, re.I):
            return name
    return ""


def guess_location(card):
    """Pull a location out of free card text for HTML sources without a location selector."""
    m = re.search(r"(US \(Remote\)|Remote(?: ?[-–] ?(?:US|USA|EST|PST|EMEA|Israel|Europe)\b)?|"
                  r"(?:Tel[- ]?Aviv|Ra'?anana|Herzliya|Haifa|Jerusalem|Petah Tikva|Ramat Gan|Giv'?atayim)(?:,? Israel)?|"
                  r"New York(?: metropolitan area(?: \([A-Z, ]+\))?)?|USA(?:, (?:West|East) Coast)?|"
                  r"Austin,? ?TX|London|Israel|United States)", card or "", re.I)
    return m.group(0).strip(" ·|-") if m else ""


def guess_workplace(card):
    """Pick up a "Full-Time ... Remote/Hybrid/On-site" label from free card text."""
    m = re.search(r"Full[- ]?Time(?:\s*Full[- ]?Time)?\s*(Remote|Hybrid|On-?site)\b", card or "", re.I)
    return {"remote": "Remote", "hybrid": "Hybrid"}.get(m.group(1).lower(), "On-site") if m else ""


def clean_location(loc):
    loc = (loc or "").strip()
    for rx, rep in LOCATION_CLEANUP:
        loc = re.sub(rx, rep, loc)
    return loc.strip(" ,·|-")


def normalize(company, raw, today, previous):
    jobs = []
    cfg = company.get("careers") or {}
    for r in raw:
        title = re.sub(r"\s+", " ", (r.get("title") or "")).strip()
        if not title or not r.get("url"):
            continue
        loc = clean_location(r.get("location") or guess_location(r.get("card", "")) or cfg.get("default_location", ""))
        # Per-company fixes for unhelpful location names, e.g. {Bigabid: Israel} in portfolio.yaml.
        aliases = {str(k).lower(): v for k, v in (cfg.get("location_aliases") or {}).items()}
        loc = aliases.get(loc.lower(), loc)
        workplace = r.get("workplace") or guess_workplace(r.get("card", ""))
        remote = workplace == "Remote" or bool(re.search(r"\bremote\b", loc, re.I))
        jid = hashlib.sha1(f"{company['name']}|{r['url']}|{title}|{loc}".encode()).hexdigest()[:12]
        prev = previous.get(jid, {})
        first_seen = prev.get("first_seen") or today
        jobs.append({
            "id": jid,
            "title": title,
            "company": company["name"],
            "url": r["url"],
            "location": loc,
            "region": region_of(loc),
            "remote": remote,
            "workplace": workplace,
            "department": re.sub(r"[‎‏]", "", (r.get("department") or "")).strip(),
            "function": classify(title, r.get("department")),
            "posted": r.get("posted") or first_seen,
            "first_seen": first_seen,
        })
    return jobs


def build(portfolio, raw_by_company, previous_doc, errors=None):
    """Assemble the jobs.json document. raw_by_company: name -> list | None (None = fetch failed)."""
    errors = errors or {}
    today = dt.date.today().isoformat()
    prev_jobs = {j["id"]: j for j in previous_doc.get("jobs", [])}
    prev_by_company = {}
    for j in previous_doc.get("jobs", []):
        prev_by_company.setdefault(j["company"], []).append(j)

    companies, jobs = [], []
    for co in portfolio["companies"]:
        name, ctype = co["name"], (co.get("careers") or {}).get("type", "none")
        raw = raw_by_company.get(name)
        if ctype == "none":
            status, cjobs = "none", []
        elif raw is None:
            status = "error"
            # Keep yesterday's jobs through short outages, but drop them after a week of failures.
            cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
            cjobs = [dict(j, stale=True, stale_since=j.get("stale_since") or today)
                     for j in prev_by_company.get(name, []) if (j.get("stale_since") or today) > cutoff]
        else:
            status, cjobs = "ok", normalize(co, raw, today, prev_jobs)
        jobs.extend(cjobs)
        companies.append({
            "name": name,
            "website": co["website"],
            "careers_url": careers_url(co),
            "sector": co.get("sector", ""),
            "fund": co.get("fund", ""),
            "description": co.get("description", ""),
            "logo": co.get("logo", ""),
            "job_count": len(cjobs),
            "status": status,
            **({"error": errors[name]} if name in errors else {}),
        })
    jobs.sort(key=lambda j: (j["posted"], j["company"], j["title"]), reverse=True)
    return {
        "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        # Jobs first seen on the baseline date were there at launch, so the board doesn't badge them "New".
        "baseline": previous_doc.get("baseline") or today,
        "job_count": len(jobs),
        "companies": companies,
        "jobs": jobs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="fetch a single company by name and print its jobs")
    args = ap.parse_args()

    portfolio = yaml.safe_load(PORTFOLIO.read_text())
    previous = json.loads(OUT.read_text()) if OUT.exists() else {}

    raw_by_company, errors = {}, {}
    for co in portfolio["companies"]:
        if args.only and co["name"].lower() != args.only.lower():
            continue
        cfg = co.get("careers") or {}
        fn = SOURCES.get(cfg.get("type"))
        if not fn:
            continue
        try:
            raw_by_company[co["name"]] = fn(cfg)
            print(f"  ok    {co['name']:<16} {len(raw_by_company[co['name']]):>3} jobs  ({cfg['type']})")
        except Exception as e:  # keep going; one bad site shouldn't stop the run
            raw_by_company[co["name"]] = None
            errors[co["name"]] = f"{type(e).__name__}: {e}"[:300]
            print(f"  FAIL  {co['name']:<16} {errors[co['name']]}", file=sys.stderr)

    if args.only:
        co = next(c for c in portfolio["companies"] if c["name"].lower() == args.only.lower())
        for j in normalize(co, raw_by_company.get(co["name"]) or [], dt.date.today().isoformat(), {}):
            print(f"{j['title']:<50} {j['location']:<22} {j['function']:<16} {j['url']}")
        return

    doc = build(portfolio, raw_by_company, previous, errors)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    # A zero-job result from a company that had many yesterday usually means its page layout changed.
    suspicious = [c["name"] for c in doc["companies"] if c["status"] == "ok" and c["job_count"] == 0
                  and any(j["company"] == c["name"] for j in previous.get("jobs", []))]
    lines = [f"**{doc['job_count']} jobs** from {sum(c['job_count'] > 0 for c in doc['companies'])} companies.", ""]
    if errors:
        lines += ["Failed (showing yesterday's jobs):", *[f"- {k}: `{v}`" for k, v in errors.items()], ""]
    if suspicious:
        lines += ["Dropped to zero jobs, check the careers page layout:", *[f"- {n}" for n in suspicious]]
    summary = "\n".join(lines)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary + "\n")


if __name__ == "__main__":
    main()
