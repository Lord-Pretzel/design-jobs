#!/usr/bin/env python3
"""Pulls job postings from each source into jobs.json (standard library only).

To add a source: write a function that returns a list of dicts from
normalize(), then add it to SOURCES at the bottom.
"""
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).with_name("jobs.json")
HEADERS = {"User-Agent": "personal-job-filter/1.0"}
# Postings older than this are dropped from jobs.json. The page's "Posted
# within" filter goes back 3 months, so this leaves a little slack.
MAX_AGE_DAYS = 120

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
US_ONLY = re.compile(r"\b(?:usa|u\.s\.?a?|united states|americas|north america)\b", re.I)
US_CITY_HINTS = (
    "new york city", "new york", "san francisco", "los angeles", "chicago",
    "austin", "seattle", "boston", "washington d.c.", "washington, d.c.",
    "denver", "atlanta", "miami", "portland", "san diego", "philadelphia",
    "dallas", "houston", "phoenix", "minneapolis", "detroit", "pittsburgh",
    "salt lake city", "raleigh", "charlotte", "nashville", "columbus",
    "cincinnati", "cleveland", "st. louis", "kansas city", "milwaukee",
    "sacramento", "san jose", "oakland", "brooklyn", "cambridge",
    "palo alto", "menlo park", "mountain view", "sunnyvale", "irvine",
    "santa monica", "culver city",
)
DESIGN_RE = re.compile(
    r"\b(?:graphic|visual|product|ux|ui|web|brand|motion|packaging|print|"
    r"interaction|digital|creative)\s*(?:design|designer)\b|"
    r"\bdesign(?:er)?\b|figma|illustrat|art director|creative director",
    re.I,
)
# Broad feeds tag almost every posting "design" (Remote OK) or keyword-match
# unrelated slugs (Himalayas' "Instructional-Designer"), so those sources are
# filtered on the job title instead.
DESIGN_TITLE_RE = re.compile(
    r"\bdesign(?:er|ers)?\b|\bux\b|\bui\b|\bgraphic\b|\bvisual\b|\billustrat|"
    r"\bbrand\b|\bpackaging\b|\bmotion\b|\btypograph|\bart director\b|"
    r"\bcreative director\b|\banimat|\bvideo editor\b|\bphotograph|\bretouch|"
    r"\bprepress\b|\bpre-press\b|\btypeset|\blayout artist\b|"
    r"\bconcept artist\b|\bstoryboard|\b3d artist\b|\bgame artist\b|"
    r"\bcreative lead\b|\bdesign strateg|\bcreative technolog",
    re.I,
)
# Aquent's feed is the agency's whole contract book, not a design feed, so it
# needs its own (wider) design-adjacent title filter. Without it the site fills
# up with pharmacists, recruiters, and Java developers.
AQUENT_TITLE_RE = re.compile(
    r"\bdesign(?:er|ers|ing)?\b|graphic|visual|\bux\b|\bui\b|illustrat|"
    r"motion graphic|animat|video edit|photo|packag|dieline|retouch|"
    r"layout|concept art|storyboard|typograph|prepress|pre[- ]press|"
    r"art director|creative director|creative lead|creative technolog|"
    r"design strateg|creative operations|user research|web design|\bartist\b",
    re.I,
)


def looks_us(loc):
    """True when a location string is US-eligible or open remote."""
    loc = (loc or "").strip()
    if not loc:
        return True
    low = loc.lower()
    if re.search(r"\b(?:remote|anywhere|worldwide|global)\b", low):
        if re.search(r"\b(?:europe|emea|apac|asia|africa|latam|latin america)\b",
                     low) and not US_ONLY.search(low):
            return False
        return True
    if US_ONLY.search(low):
        return True
    if any(re.search(rf"\b{re.escape(name)}\b", low) for name in NAMES):
        return True
    m = re.search(r",\s*([A-Z]{2})\b", loc)
    if m and m[1] in CODES:
        return True
    return any(city in low for city in US_CITY_HINTS)


def is_design(title, tags="", desc=""):
    """Loose design filter for broad feeds (Remote OK, Jobicy, Himalayas)."""
    if DESIGN_RE.search(title or "") or DESIGN_RE.search(tags or ""):
        return True
    return bool(re.search(
        r"\b(?:graphic|ux|ui|product|brand|web|motion|packaging|visual)\s+design\b",
        desc or "", re.I))


def money(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0


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
    pay = _pay_text(d, desc)
    # Some feeds leave template placeholders ("[[cust_salaryMin]]") in the text.
    return "" if re.search(r"\[\[|\]\]|\{\{", pay) else pay


def _pay_text(d, desc):
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
        r"flexible|remote|virtual|anywhere|worldwide|global|work from home|[\W_]+",
        "", loc, flags=re.I))
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
    out = []
    for e in els:
        j = normalize(flat(e), "Aquent")
        if j and j["title"] and AQUENT_TITLE_RE.search(j["title"]):
            out.append(j)
    return out


def get_json(url):
    return json.loads(fetch(url))


def fetch_weworkremotely():
    """We Work Remotely's public design-category RSS feed."""
    raw = fetch("https://weworkremotely.com/categories/remote-design-jobs.rss")
    out = []
    for item in ET.fromstring(raw).iter():
        if local(item.tag) != "item":
            continue
        d = flat(item)
        title = clean(d.get("title", ""))
        company, sep, role = title.partition(": ")
        if not sep:
            company, role = "We Work Remotely", title
        region = d.get("region", "")
        low = region.lower()
        us = bool(re.search(r"usa|united states|u\.s\.|americas|north america", low))
        world = bool(re.search(r"anywhere|worldwide|global", low))
        if region and not us and not world:
            continue
        if not DESIGN_TITLE_RE.search(role):
            continue
        out.append({
            "id": d.get("guid") or d.get("link", ""),
            "title": clean(role), "url": d.get("link", ""),
            "company": clean(company), "location": "Remote",
            "country": "US" if us else "", "state": d.get("state", ""),
            "workplace": "remote", "jobtype": d.get("type", ""),
            "description": d.get("description", ""), "date": d.get("pubdate", ""),
        })
    return [j for j in (normalize(d, "We Work Remotely") for d in out) if j and j["title"]]


def fetch_dribbble():
    """Dribbble's public jobs RSS feed."""
    raw = fetch("https://dribbble.com/jobs.rss")
    out = []
    for item in ET.fromstring(raw).iter():
        if local(item.tag) != "item":
            continue
        d = flat(item)
        title = clean(d.get("title", ""))
        m = re.match(r"^(.*?) is hiring for a position of (.*)$", title, re.I)
        if m:
            company, rest = clean(m[1]), clean(m[2])
            role, loc = (rest.rsplit(" in ", 1) if " in " in rest else (rest, ""))
            if role.lower().endswith(" anywhere"):
                role, loc = role[:-9].strip(), "Remote"
        else:
            company, role, loc = clean(d.get("creator", "")), title, ""
        if not role or "closed" in role.lower():
            continue
        if not DESIGN_TITLE_RE.search(role):
            continue
        loc = clean(loc)
        if loc and not looks_us(loc):
            continue
        remote = not loc or bool(re.search(r"remote|anywhere", loc, re.I))
        out.append({
            "id": d.get("guid") or d.get("link", ""),
            "title": clean(role), "url": d.get("link", ""),
            "company": company, "location": "Remote" if remote else loc,
            "country": "US" if looks_us(loc) else "",
            "workplace": "remote" if remote else "",
            "description": "", "date": d.get("pubdate", ""),
        })
    return [j for j in (normalize(d, "Dribbble") for d in out) if j and j["title"]]


def fetch_remoteok():
    """Remote OK public API. The footer links back to Remote OK as required."""
    out = []
    for r in get_json("https://remoteok.com/api"):
        if not isinstance(r, dict) or not r.get("id") or not r.get("position"):
            continue
        title = clean(r.get("position", ""))
        tags = " ".join(str(t).lower() for t in (r.get("tags") or []))
        # Remote OK tags nearly every posting "design", so filter on the title.
        if not DESIGN_TITLE_RE.search(title):
            continue
        loc = (r.get("location") or "").strip()
        if not looks_us(loc):
            continue
        lo, hi = money(r.get("salary_min")), money(r.get("salary_max"))
        out.append({
            "id": str(r["id"]), "title": title,
            "url": r.get("apply_url") or r.get("url", ""),
            "company": clean(r.get("company", "")),
            "location": "Remote" if not loc or re.search(
                r"worldwide|anywhere", loc, re.I) else loc,
            "country": "US" if looks_us(loc) else "",
            "workplace": "remote", "jobtype": tags,
            "salary": f"${lo:,.0f}-{hi:,.0f} / year" if lo and hi else "",
            "description": r.get("description", ""), "date": r.get("date", ""),
        })
    return [j for j in (normalize(d, "Remote OK") for d in out) if j and j["title"]]


JOBICY_INDUSTRIES = ("design-multimedia", "web-app-design")


def fetch_jobicy():
    """Jobicy public API, US remote jobs in its two design industries.

    The footer credits Jobicy and every link goes to the Jobicy posting, as
    their API terms require.
    """
    out, seen = [], set()
    for industry in JOBICY_INDUSTRIES:
        url = f"https://jobicy.com/api/jobs?count=100&geo=usa&industry={industry}"
        for r in get_json(url).get("jobs", []):
            jid = str(r.get("id", ""))
            if not jid or jid in seen:
                continue
            seen.add(jid)
            title = clean(r.get("jobTitle", ""))
            industries = " ".join(r.get("jobIndustry") or [])
            desc = r.get("jobDescription", "")
            if not is_design(title, industries, desc[:400]):
                continue
            geo = r.get("jobGeo", "")
            if not looks_us(geo):
                continue
            cur = r.get("salaryCurrency") or ""
            lo, hi = money(r.get("salaryMin")), money(r.get("salaryMax"))
            period = r.get("salaryPeriod") or ""
            salary = f"{cur} {lo:,.0f}-{hi:,.0f} / {period}".strip() if lo and hi else ""
            out.append({
                "id": jid, "title": title,
                "url": r.get("url", ""), "company": clean(r.get("companyName", "")),
                "location": "Remote", "country": "US",
                "workplace": "remote", "jobtype": " ".join(r.get("jobType") or []),
                "salary": salary, "description": desc, "date": r.get("pubDate", ""),
            })
        time.sleep(0.5)
    return [j for j in (normalize(d, "Jobicy") for d in out) if j and j["title"]]


HIMALAYAS_SEARCHES = [("designer", 3), ("design", 2)]


def fetch_himalayas():
    """Himalayas search API for US-eligible design roles.

    Links use the posting's applicationLink unchanged, per their API terms.
    """
    out, seen = [], set()
    for query, pages in HIMALAYAS_SEARCHES:
        for page in range(1, pages + 1):
            url = (f"https://himalayas.app/jobs/api/search?q={query}"
                   f"&country=US&sort=recent&page={page}")
            try:
                jobs = get_json(url).get("jobs", [])
            except Exception as e:
                print(f"Himalayas {query} page {page} failed: {e}", file=sys.stderr)
                break
            for r in jobs:
                jid = str(r.get("guid") or r.get("applicationLink") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                title = clean(r.get("title", ""))
                desc = r.get("description", "")
                if not DESIGN_TITLE_RE.search(title):
                    continue
                locs = r.get("locationRestrictions") or []
                if locs and not any(looks_us(x) for x in locs):
                    continue
                cur = r.get("currency") or ""
                lo, hi = money(r.get("minSalary")), money(r.get("maxSalary"))
                period = r.get("salaryPeriod") or ""
                salary = f"{cur} {lo:,.0f}-{hi:,.0f} / {period}".strip() if lo and hi else ""
                ts = r.get("pubDate")
                date = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                        if isinstance(ts, (int, float)) else "")
                out.append({
                    "id": jid, "title": title,
                    "url": r.get("applicationLink", ""),
                    "company": clean(r.get("companyName", "")),
                    "location": "Remote", "country": "US",
                    "workplace": "remote", "jobtype": r.get("employmentType", ""),
                    "salary": salary, "description": desc, "date": date,
                })
            time.sleep(0.5)
            if len(jobs) < 20:
                break
    return [j for j in (normalize(d, "Himalayas") for d in out) if j and j["title"]]


ADZUNA_SEARCHES = ["graphic designer", "ux designer", "ui designer",
                   "product designer", "packaging designer", "web designer"]
# Recruiters are found by company name: Adzuna's search has a company filter.
ADZUNA_RECRUITERS = ["Creative Circle", "The Creative Group", "Artisan Creative",
                     "Mondo", "Creative Financial Staffing"]


def fetch_adzuna():
    """Adzuna API (free key). About 17 calls per run, well inside the free quota."""
    from urllib.parse import urlencode
    app = {"app_id": os.environ["ADZUNA_APP_ID"], "app_key": os.environ["ADZUNA_APP_KEY"]}
    jobs = [({"what": w}, 2) for w in ADZUNA_SEARCHES]
    jobs += [({"what": "design", "company": c}, 1) for c in ADZUNA_RECRUITERS]
    seen, out, errors = set(), [], 0
    for extra, pages in jobs:
        got = 0
        for page in range(1, pages + 1):
            q = {**app, **extra, "results_per_page": 50, "max_days_old": 30,
                 "sort_by": "date", "content-type": "application/json"}
            try:
                data = get_json(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}?{urlencode(q)}")
            except Exception as e:
                print(f"Adzuna {extra} page {page} failed: {e}", file=sys.stderr)
                errors += 1
                break
            time.sleep(0.5)
            for r in data.get("results", []):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                area = (r.get("location") or {}).get("area") or []
                lo, hi = r.get("salary_min"), r.get("salary_max")
                stated = lo and hi and str(r.get("salary_is_predicted")) != "1"
                out.append({
                    "id": str(r["id"]), "title": clean(r.get("title", "")),
                    "url": r.get("redirect_url", ""),
                    "company": (r.get("company") or {}).get("display_name", ""),
                    "location": (r.get("location") or {}).get("display_name", ""),
                    "state": area[1] if len(area) > 1 else "",
                    "city": area[-1] if len(area) > 2 else "", "country": "US",
                    "description": r.get("description", ""), "date": r.get("created", ""),
                    "jobtype": f"{r.get('contract_type', '')} {r.get('contract_time', '')}",
                    "salary": f"${lo:,.0f}-{hi:,.0f} / year" if stated else ""})
                got += 1
        if "company" in extra:
            print(f"Adzuna: {got} new postings for {extra['company']}")
    if errors == len(jobs) * 1 and not out:
        raise RuntimeError("Every Adzuna request failed (check the key and quota)")
    return [j for j in (normalize(d, "Adzuna") for d in out) if j and j["title"]]


# Recruiter sites (Creative Circle and others) come in through Adzuna above.
SOURCES = {
    "Aquent": fetch_aquent,
    "We Work Remotely": fetch_weworkremotely,
    "Dribbble": fetch_dribbble,
    "Remote OK": fetch_remoteok,
    "Jobicy": fetch_jobicy,
    "Himalayas": fetch_himalayas,
}
if os.environ.get("ADZUNA_APP_ID") and os.environ.get("ADZUNA_APP_KEY"):
    SOURCES["Adzuna"] = fetch_adzuna


def main():
    try:
        prev = json.loads(OUT.read_text())
    except (OSError, ValueError):
        prev = None
    prev_jobs = {j["id"]: j for j in (prev or {}).get("jobs", [])}
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    jobs, counts = {}, {}
    for name, fn in SOURCES.items():
        try:
            got = fn()
        except Exception as e:  # keep yesterday's data for a source that fails
            print(f"{name} failed: {e}", file=sys.stderr)
            got = [j for j in prev_jobs.values() if j["source"] == name]
            if name == "Aquent":  # keep the design-only rule on cached data too
                got = [j for j in got if AQUENT_TITLE_RE.search(j["title"])]
        counts[name] = len(got)
        for j in got:
            old = prev_jobs.get(j["id"])
            j["first_seen"] = old["first_seen"] if old else (today if prev else "")
            jobs[j["id"]] = j

    # Keep the file small: the page's "Posted within" filter only goes back to
    # 3 months, so anything older than this window is dropped here.
    cutoff = (now.date() - timedelta(days=MAX_AGE_DAYS)).isoformat()
    stale = [jid for jid, j in jobs.items() if j["posted"] and j["posted"] < cutoff]
    for jid in stale:
        del jobs[jid]
    counts = {name: 0 for name in SOURCES}
    for j in jobs.values():
        counts[j["source"]] += 1

    OUT.write_text(json.dumps({
        "updated": now.isoformat(timespec="seconds"),
        "counts": counts,
        "jobs": list(jobs.values()),
    }, indent=1))
    print(f"Saved {len(jobs)} jobs {counts}"
          + (f" (dropped {len(stale)} older than {MAX_AGE_DAYS} days)" if stale else ""))
    # One dead feed must not stop the workflow from committing the other sources.
    sys.exit(0 if jobs else 1)


if __name__ == "__main__":
    main()
