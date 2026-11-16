#!/usr/bin/env python3
"""Pulls job postings from each source into jobs.json (standard library only).

To add a source: write a function that returns a list of dicts from
normalize(), then add it to SOURCES at the bottom.
"""
import email.utils
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).with_name("jobs.json")
HEADERS = {"User-Agent": "personal-job-filter/1.0"}

_STATES = (
    "Alabama:AL,Alaska:AK,Arizona:AZ,Arkansas:AR,California:CA,Colorado:CO,"
    "Connecticut:CT,Delaware:DE,District of Columbia:DC,Florida:FL,Georgia:GA,"
    "Hawaii:HI,Idaho:ID,Illinois:IL,Indiana:IN,Iowa:IA,Kansas:KS,Kentucky:KY,"
    "Louisiana:LA,Maine:ME,Maryland:MD,Massachusetts:MA,Michigan:MI,"
    "Minnesota:MN,Mississippi:MS,Missouri:MO,Montana:MT,Nebraska:NE,Nevada:NV,"
    "New Hampshire:NH,New Jersey:NJ,New Mexico:NM,New York:NY,"
    "North Carolina:NC,North Dakota:ND,Ohio:OH,Oklahoma:OK,Oregon:OR,"
    "Pennsylvania:PA,Rhode Island:RI,South Carolina:SC,South Dakota:SD,"
    "Tennessee:TN,Texas:TX,Utah:UT,Vermont:VT,Virginia:VA,Washington:WA,"
    "West Virginia:WV,Wisconsin:WI,Wyoming:WY"
)
NAMES = {p.split(":")[0].lower(): p.split(":")[1] for p in _STATES.split(",")}
CODES = set(NAMES.values())
US_NAMES = {"us", "usa", "united states", "united states of america"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def local(tag):
    return tag.split("}")[-1].lower()


def flat(el):
    """Map every descendant tag name (lowercase) to its first non-empty text."""
    d = {}
    for c in el.iter():
        if c is el:
            continue
        t = (c.text or "").strip()
        k = local(c.tag)
        if t and k not in d:
            d[k] = t
    return d


def pick(d, *keys):
    for k in keys:
        if d.get(k):
            return d[k]
    return ""


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", html.unescape(text))
    return re.sub(r"\s+", " ", text).strip()


def to_state(s):
    s = (s or "").strip()
    if len(s) == 2 and s.upper() in CODES:
        return s.upper()
    return NAMES.get(s.lower(), "")


def to_date(s):
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s[:10]).date().isoformat()
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return ""


def normalize(d, source):
    """Turn one raw posting (flat dict) into the shape the web page expects."""
    title = pick(d, "title", "jobtitle", "job_title", "positiontitle")
    url = pick(d, "url", "link", "applyurl", "apply_url", "joburl")
    desc = clean(pick(d, "description", "summary", "body", "jobdescription"))
    city = pick(d, "city")
    state = to_state(pick(d, "state", "region", "province", "statecode"))
    loc = pick(d, "location", "joblocation", "locations")
    country = pick(d, "country", "countrycode").strip().lower()

    if loc and not (city and state):
        m = re.match(r"\s*([^,]+?),\s*([A-Za-z .]+?)(?:,|$)", loc)
        if m:
            city = city or m.group(1).strip()
            state = state or to_state(m.group(2))

    field = pick(d, "workplace", "workplacetype", "remotetype", "remote",
                 "locationtype", "jobmode")
    text = " ".join([field, title, loc, desc[:1500]]).lower()
    if "hybrid" in text:
        workplace = "hybrid"
    elif re.search(r"\bremote\b|work from home|telecommut", text):
        workplace = "remote"
    elif city or state:
        workplace = "onsite"
    else:
        workplace = "unknown"

    # US only: keep US states, explicit US country, or remote with no location.
    in_us = bool(state) or country in US_NAMES or "united states" in loc.lower()
    if not in_us and not (workplace == "remote" and not (loc or country)):
        return None

    raw_type = " ".join([pick(d, "jobtype", "employmenttype", "employment_type",
                              "type", "worktype", "placementtype"),
                         desc[:300]]).lower()
    if re.search(r"\b(contract|temporary|temp|freelance)\b", raw_type):
        jtype = "Contract"
    elif "part" in raw_type and "time" in raw_type:
        jtype = "Part-time"
    elif re.search(r"full[- ]?time|perm", raw_type):
        jtype = "Full-time"
    else:
        jtype = "Unknown"

    job_id = pick(d, "id", "jobid", "referencenumber", "reference", "guid") or url
    return {
        "id": f"{source}:{job_id}",
        "source": source,
        "title": title,
        "company": pick(d, "company", "employer", "companyname") or source,
        "url": url,
        "city": city,
        "state": state,
        "workplace": workplace,
        "type": jtype,
        "posted": to_date(pick(d, "date", "postdate", "posteddate", "posted",
                               "pubdate", "publisheddate", "datecreated",
                               "dateposted", "createddate")),
        "pay": pick(d, "salary", "compensation", "pay", "payrate", "salaryrange"),
        "description": desc[:600],
    }


def find_jobs(root):
    """Find the repeated XML element that holds one posting each."""
    title_keys = ("title", "jobtitle", "job_title", "positiontitle")
    counts = Counter(local(e.tag) for e in root.iter() if e is not root and len(e))
    for tag, n in counts.most_common():
        if n < 3:
            break
        els = [e for e in root.iter() if local(e.tag) == tag]
        if all(any(k in flat(s) for k in title_keys) for s in els[:3]):
            return els
    return []


def fetch_aquent():
    raw = fetch("https://aquent.com/feeds/jobs.xml")
    els = find_jobs(ET.fromstring(raw))
    if not els:
        print(raw[:1500].decode("utf-8", "replace"))
        raise RuntimeError("Could not find job entries in the Aquent feed")
    return [j for j in (normalize(flat(e), "Aquent") for e in els) if j and j["title"]]


# Creative Circle has no public feed that I know of. Add a function here once
# you have a page or feed to read, then list it in SOURCES.
SOURCES = {
    "Aquent": fetch_aquent,
}


def main():
    try:
        prev = json.loads(OUT.read_text())
    except (OSError, ValueError):
        prev = None
    prev_jobs = {j["id"]: j for j in (prev or {}).get("jobs", [])}
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    jobs, counts, failed = {}, {}, False
    for name, fn in SOURCES.items():
        try:
            got = fn()
        except Exception as e:  # keep yesterday's data for a source that fails
            print(f"{name} failed: {e}", file=sys.stderr)
            failed = True
            got = [j for j in prev_jobs.values() if j["source"] == name]
        counts[name] = len(got)
        for j in got:
            old = prev_jobs.get(j["id"])
            j["first_seen"] = old["first_seen"] if old else (today if prev else "")
            jobs[j["id"]] = j

    OUT.write_text(json.dumps({
        "updated": now.isoformat(timespec="seconds"),
        "counts": counts,
        "jobs": list(jobs.values()),
    }, indent=1))
    print(f"Saved {len(jobs)} jobs {counts}")
    sys.exit(1 if failed and not jobs else 0)


if __name__ == "__main__":
    main()
