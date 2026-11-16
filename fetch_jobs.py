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


TAGS = [
    ("Figma", r"figma"),
    ("Adobe CC", r"photoshop|illustrator|indesign|after effects|premiere|adobe"),
    ("Prototyping", r"prototyp"), ("Design systems", r"design systems?"),
    ("Accessibility", r"accessib|wcag"), ("Packaging", r"packag(e|ing)|dieline"),
    ("Branding", r"\bbrand(ing)?\b"), ("Illustration", r"illustrat"),
    ("Motion", r"motion|animation"), ("Print", r"\bprint\b|pre-?press"),
    ("Social", r"social media|social content"), ("Email", r"\bemail\b"),
    ("Presentations", r"presentation|pitch deck|powerpoint|keynote"),
    ("E-commerce", r"e-?commerce|amazon|shopify|product pages"),
    ("Web", r"\bweb(site)?s?\b|webflow|wordpress"),
    ("3D", r"\b3d\b|blender|cinema 4d"), ("Retouching", r"retouch"),
    ("Art direction", r"art direct"), ("Editorial", r"editorial|magazine"),
    ("Apparel", r"apparel|fashion|footwear|accessories"),
]


def extract_tags(title, desc):
    found = [n for n, p in TAGS if re.search(p, title, re.I)]
    found += [n for n, p in TAGS if n not in found and re.search(p, desc, re.I)]
    return found[:5]


LABELS = r"(?:placement type|salary|pay rate|start date|duration|location|overview|hours|contract length)"
DATE_FORMATS = ("%Y-%m-%d", "%m.%d.%Y", "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%y",
                "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")


def label(desc, name):
    """Value after a 'Label:' in the posting text, up to the next known label."""
    m = re.search(rf"\b{name}\s*:\s*(.+?)(?=\s+{LABELS}\s*:|$)", desc, re.I)
    return m.group(1).strip()[:80] if m else ""


def fuzzy(d, pattern):
    """First non-empty value whose field name matches the pattern."""
    for k, v in d.items():
        if v and re.search(pattern, k, re.I):
            return v
    return ""


def parse_any_date(s):
    s = re.sub(r"^([A-Za-z]{3,9})\.", r"\1", (s or "").strip())
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    if re.match(r"(asap|immediate)", s, re.I):
        return "ASAP"
    for f in DATE_FORMATS:
        for cand in (s, s[:10]):
            try:
                return datetime.strptime(cand, f).date().isoformat()
            except ValueError:
                pass
    return ""


def extract_dates(desc, start_hint="", duration_hint=""):
    """Start date and duration: from labeled fields first, then the posting text."""
    start = parse_any_date(start_hint)
    if not start:
        m = re.search(r"start(?:\s*date)?\s*[:\-]?\s*([A-Za-z]{3,9}\.? \d{1,2},? \d{4}"
                      r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|ASAP|immediate\w*)", desc, re.I)
        start = parse_any_date(m[1]) if m else ""
    if duration_hint:
        return start, duration_hint[:30]
    m = re.search(r"(\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?\s*(?:weeks?|months?))", desc, re.I)
    if m:
        duration = m[1].strip()
    else:
        duration = "Ongoing" if re.search(r"\b(ongoing|long[- ]term)\b", desc, re.I) else ""
    return start, duration


def pay_text(d, desc):
    """Readable pay, e.g. '$55-58 Hourly'. Hides bare numbers with no unit."""
    t = label(desc, r"(?:salary|pay rate)")
    if t:
        return t
    unit = fuzzy(d, r"(period|unit|interval|frequency|basis)")
    lo = fuzzy(d, r"(salary|pay|rate|comp).*min|min.*(salary|pay|rate|comp)")
    hi = fuzzy(d, r"(salary|pay|rate|comp).*max|max.*(salary|pay|rate|comp)")
    if lo and hi and unit:
        return f"${lo}-{hi} / {unit}"
    v = pick(d, "salary", "compensation", "pay", "payrate", "salaryrange", "rate")
    if v and not re.fullmatch(r"[\d.,]+", v.strip()):
        return v
    if v and unit:
        return f"${v} / {unit}"
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
    # Remote with no real place named ("Remote; Virtual", "Flexible / Remote").
    open_remote = (workplace == "remote" and not country and not re.sub(
        r"flexible|remote|virtual|anywhere|work from home|[\W_]+", "", loc, flags=re.I))
    if not in_us and not open_remote:
        return None

    raw_type = " ".join([pick(d, "jobtype", "employmenttype", "employment_type",
                              "type", "worktype", "placementtype"),
                         fuzzy(d, r"placement|employment|job_?type|work_?type"),
                         label(desc, "placement type"), desc[:300]]).lower()
    if re.search(r"\b(contract|temporary|temp|freelance)\b", raw_type):
        jtype = "Contract"
    elif "part" in raw_type and "time" in raw_type:
        jtype = "Part-time"
    elif re.search(r"full[-_ ]?time|perm", raw_type):
        jtype = "Full-time"
    else:
        jtype = "Unknown"

    job_id = pick(d, "id", "jobid", "referencenumber", "reference", "guid") or url
    start, duration = extract_dates(
        desc,
        label(desc, "start date") or fuzzy(d, r"start"),
        label(desc, "duration") or fuzzy(d, r"duration|length|term"))
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
        "pay": pay_text(d, desc),
        "start": start,
        "duration": duration,
        "tags": extract_tags(title, desc),
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
    print("Sample Aquent posting fields:",
          json.dumps({k: v[:120] for k, v in flat(els[0]).items()}, indent=1))
    return [j for j in (normalize(flat(e), "Aquent") for e in els) if j and j["title"]]


def get_json(url):
    return json.loads(fetch(url))


def fetch_muse():
    """The Muse public API, Design and UX category."""
    out = []
    for page in range(1, 6):
        data = get_json("https://www.themuse.com/api/public/jobs"
                        f"?category=Design%20and%20UX&page={page}")
        for r in data.get("results", []):
            locs = r.get("locations") or [{}]
            out.append({"id": str(r.get("id")), "title": r.get("name", ""),
                        "url": (r.get("refs") or {}).get("landing_page", ""),
                        "company": (r.get("company") or {}).get("name", ""),
                        "location": locs[0].get("name", ""),
                        "description": r.get("contents", ""),
                        "date": r.get("publication_date", "")})
        if page >= data.get("page_count", 1):
            break
    return [j for j in (normalize(d, "The Muse") for d in out) if j and j["title"]]


def fetch_remotive():
    """Remotive public API, design category (remote roles)."""
    out = []
    for r in get_json("https://remotive.com/api/remote-jobs?category=design").get("jobs", []):
        loc = r.get("candidate_required_location", "")
        us = re.search(r"usa|united states|\bus\b|north america|americas", loc, re.I)
        if not us and not re.search(r"worldwide|anywhere", loc, re.I):
            continue
        out.append({"id": str(r.get("id")), "title": r.get("title", ""),
                    "url": r.get("url", ""), "company": r.get("company_name", ""),
                    "workplace": "remote", "country": "USA" if us else "",
                    "jobtype": r.get("job_type", ""), "salary": r.get("salary", ""),
                    "description": r.get("description", ""),
                    "date": r.get("publication_date", "")})
    return [j for j in (normalize(d, "Remotive") for d in out) if j and j["title"]]


# Creative Circle has no public feed that I know of. Add a function here once
# you have a page or feed to read, then list it in SOURCES.
SOURCES = {
    "Aquent": fetch_aquent,
    "The Muse": fetch_muse,
    "Remotive": fetch_remotive,
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
