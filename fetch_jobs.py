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
from concurrent.futures import ThreadPoolExecutor
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
    r"\bcreative lead\b|\bdesign strateg|\bcreative technolog|\b3d\b|wayfinding|"
    r"signage|experiential|\bexhibit",
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
    r"design strateg|creative operations|user research|web design|\bartist\b|"
    r"\b3d\b|wayfinding|signage|experiential|exhibit|environmental",
    re.I,
)
# Hardware engineering slips past the rule above when the role name itself
# contains "design" ("EE Switches Design Responsible Engineer").
AQUENT_SKIP_RE = re.compile(
    r"responsible engineer|\bconnector\b|semiconductor|\bsilicon\b|"
    r"\bfirmware\b|\bpcb\b|\bfpga\b|electrical engineer|mechanical engineer",
    re.I,
)

# Design in the CAD, chip, construction and learning-and-development senses.
NON_GRAPHIC_RE = re.compile(
    r"\bpcb\b|\bsram\b|layout design engineer|mechanical|\bbim\b|\bvdc\b|"
    r"virtual design and construction|sprinkler|instructional|learning design|"
    r"\bcivil\b|\belectrical\b|\bhvac\b|plumbing|piping|\bsteel\b|process design|"
    r"task designer|terminal-bench|verification|firmware|semiconductor|\bchip\b|"
    r"\basic\b|\bfpga\b|\bcad\b|drafter|drafting",
    re.I,
)
AQUENT_SKIP_RE = re.compile(AQUENT_SKIP_RE.pattern + "|" + NON_GRAPHIC_RE.pattern, re.I)
OPEN_FEEDS = {"Himalayas", "Remote OK", "We Work Remotely", "Dribbble", "Jobicy"}


def title_ok(source, title):
    """One shared title rule for every source."""
    if NON_GRAPHIC_RE.search(title or ""):
        return False
    return not (source in OPEN_FEEDS and OPEN_SKIP_RE.search(title or ""))


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


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def extract_dates(desc, start_hint="", duration_hint="", contract=False):
    """Start date and duration. Duration is only read for contract-style roles,
    and only next to words like contract or assignment, so parental leave,
    vacation and vesting periods are never mistaken for it."""
    start = parse_any_date(start_hint)
    if not start:
        m = re.search(r"start(?:\s*date)?\s*[:\-]?\s*([A-Za-z]{3,9}\.? \d{1,2},? \d{4}"
                      r"|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|ASAP|immediate\w*)", desc, re.I)
        start = parse_any_date(m[1]) if m else ""
    if duration_hint:
        return start, duration_hint[:30].strip(" .;,")
    if not contract:
        return start, ""
    num = r"(\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?\s*(?:weeks?|months?))"
    m = (re.search(r"(?:contract|assignment|engagement|duration|term|project)"
                   r"(?:\s+(?:length|duration|term))?\s*(?:of|is|:|-|–)?\s*"
                   r"(?:approximately|approx\.?|about|up to)?\s*" + num, desc, re.I)
         or re.search(num + r"\s*(?:contract|assignment|engagement|project|temp)", desc, re.I))
    if m:
        return start, m[1].strip()
    if re.search(r"\b(?:ongoing|long[- ]term)\s+(?:contract|assignment|engagement)|"
                 r"(?:contract|temp)[- ]to[- ]hire", desc, re.I):
        return start, "Ongoing"
    return start, ""


def pay_text(d, desc):
    """Readable pay, e.g. '$55-58 Hourly'. Hides bare numbers with no unit."""
    pay = _pay_text(d, desc)
    # Some feeds leave template placeholders ("[[cust_salaryMin]]") in the text.
    return "" if re.search(r"\[\[|\]\]|\{\{", pay) else pay


MONEY_RX = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*[kK]?"
    r"(?:\s*(?:-|–|—|to)\s*\$?\s?\d[\d,]*(?:\.\d+)?\s*[kK]?)?"
    r"(?:\s*(?:USD|per hour|/\s*hour|/\s*hr|hourly|per year|annually|/\s*year|/\s*yr|a year|an hour))?",
    re.I)


def _pay_text(d, desc):
    t = label(desc, r"(?:salary|pay rate)")
    m = MONEY_RX.search(t) if t else None
    if m:
        return m.group(0).strip()
    unit = fuzzy(d, r"(period|unit|interval|frequency|basis)")
    lo = fuzzy(d, r"(salary|pay|rate|comp).*min|min.*(salary|pay|rate|comp)")
    hi = fuzzy(d, r"(salary|pay|rate|comp).*max|max.*(salary|pay|rate|comp)")
    if lo and hi and unit:
        return f"${lo}-{hi} / {unit}"
    v = pick(d, "salary", "compensation", "pay", "payrate", "salaryrange", "rate")
    if v and not re.fullmatch(r"[\d.,]+", v.strip()):
        m = MONEY_RX.search(v)
        if m:
            return m.group(0).strip()
        return v if len(v) <= 40 else ""
    if v and unit:
        return f"${v} / {unit}"
    return ""


US_HINT = re.compile(
    r"united states|\busa\b|\bu\.s\.|\bus\b|north america|\bamericas\b|nationwide",
    re.I,
)

# Some boards give a city with no state ("Los Angeles"). This fills the gap so
# the State filter can see those roles. Only names that are unambiguous in a US
# job posting, and only used when the source left the state blank.
CITY_STATE = {
    "los angeles": "CA", "san francisco": "CA", "san diego": "CA", "san jose": "CA",
    "san mateo": "CA", "san bruno": "CA", "santa monica": "CA", "culver city": "CA",
    "palo alto": "CA", "mountain view": "CA", "menlo park": "CA", "foster city": "CA",
    "oakland": "CA", "berkeley": "CA", "sacramento": "CA", "new york": "NY",
    "new york city": "NY", "brooklyn": "NY", "chicago": "IL", "boston": "MA",
    "seattle": "WA", "austin": "TX", "dallas": "TX", "houston": "TX",
    "san antonio": "TX", "denver": "CO", "atlanta": "GA", "philadelphia": "PA",
    "pittsburgh": "PA", "washington": "DC", "miami": "FL", "orlando": "FL",
    "tampa": "FL", "phoenix": "AZ", "detroit": "MI", "cleveland": "OH",
    "columbus": "OH", "cincinnati": "OH", "nashville": "TN", "charlotte": "NC",
    "raleigh": "NC", "minneapolis": "MN", "st. louis": "MO", "milwaukee": "WI",
    "indianapolis": "IN", "salt lake city": "UT", "las vegas": "NV",
    "portland": "OR", "baltimore": "MD", "richmond": "VA", "memphis": "TN",
}


# Non-US roles are kept only from sources that publish an employer's own postings
# (company career boards, Aquent, Coroflot). Open aggregators stay US-only.
NONUS_SOURCES = {"Aquent", "Company boards", "Coroflot"}
_COUNTRIES = [
    ("Canada", r"canada|\b(?:ontario|quebec|british columbia|alberta|manitoba|toronto|vancouver|montreal|ottawa|calgary)\b"),
    ("United Kingdom", r"united kingdom|\buk\b|\bu\.k\.|england|scotland|wales|northern ireland|\blondon\b|manchester|edinburgh|glasgow|bristol|leeds"),
    ("Ireland", r"\bireland\b|\bdublin\b"),
    ("Australia", r"australia|sydney|melbourne|brisbane|\bperth\b|canberra"),
    ("New Zealand", r"new zealand|auckland"),
    ("Singapore", r"singapore"),
    ("India", r"\bindia\b|bangalore|bengaluru|mumbai|\bdelhi\b|hyderabad|\bpune\b|chennai"),
    ("South Africa", r"south africa|cape town|johannesburg"),
    ("Germany", r"germany|\bberlin\b|munich|hamburg"),
    ("Netherlands", r"netherlands|amsterdam|rotterdam"),
    ("France", r"\bfrance\b|\bparis\b"),
    ("Spain", r"\bspain\b|madrid|barcelona"),
    ("Portugal", r"portugal|lisbon"),
    ("Sweden", r"sweden|stockholm"),
    ("Denmark", r"denmark|copenhagen"),
    ("Switzerland", r"switzerland|zurich|geneva"),
    ("Poland", r"poland|warsaw|krakow"),
    ("Philippines", r"philippines|\bmanila\b"),
    ("Mexico", r"\bmexico\b"),
    ("Brazil", r"brazil|sao paulo"),
    ("Japan", r"\bjapan\b|tokyo"),
    ("United Arab Emirates", r"united arab emirates|\buae\b|dubai|abu dhabi"),
    ("Israel", r"\bisrael\b|tel aviv"),
]
COUNTRY_RX = [(n, re.compile(p, re.I)) for n, p in _COUNTRIES]
ENGLISH_WORDS = re.compile(r"\b(?:the|and|to|of|a|in|for|with|you|our|will|we|is|are)\b", re.I)
SCAM_RX = re.compile(
    r"whatsapp|telegram|western union|wire transfer|gift cards?|processing fee|training fee|"
    r"crypto(?:currency)? payment|@(?:gmail|yahoo|outlook|hotmail|proton\w*)\.|"
    r"earn \$?\d[\d,]*\s*(?:per|a|/)\s*(?:day|week)|"
    r"no experience (?:needed|required).{0,40}(?:earn|weekly|daily)", re.I)


def detect_country(loc, country_field=""):
    """Name of a non-US country found in a location string, else ''."""
    text = f"{loc} {country_field}"
    for name, rx in COUNTRY_RX:
        if rx.search(text):
            return name
    return ""


def looks_english(text):
    words = re.findall(r"[A-Za-z\u00C0-\u00FF']+", text[:2000])
    if len(words) < 12:
        return True  # too short to judge; the title and place already read as English
    return len(ENGLISH_WORDS.findall(" ".join(words))) / len(words) >= 0.18


def scam_signals(text):
    return bool(SCAM_RX.search(text))


def fix_text(t):
    """Unescape HTML entities and repair text that was decoded with the wrong encoding."""
    t = html.unescape(t or "")
    try:
        t = t.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return re.sub(r"\s+", " ", t).strip()


def ends(text, head, tail):
    """Start and end of a long text. The end usually holds requirements and pay."""
    return text if len(text) <= head + tail else text[:head] + " … " + text[-tail:]


INDUSTRIES = [
    ("Healthcare & Wellness", 1.0, r"health ?care|pharma|biotech|medical|hospital|clinical|patient|therapeutic|medicine|health system|digital health"),
    ("Finance & Insurance", 1.0, r"financ|\bbank|fintech|insurance|invest|wealth|payments?\b|credit|lending|mortgage"),
    ("Technology & Software", 0.6, r"software|saas|\bb2b\b|cloud|\bai\b|artificial intelligence|machine learning|tech(?:nology)? company|startup|\bapp\b"),
    ("Retail & E-commerce", 1.0, r"retail|e-?commerce|shopify|marketplace|storefront|online store"),
    ("Consumer Goods", 1.0, r"consumer goods|\bcpg\b|packaged goods|\bfood\b|beverage|beauty|skincare|cosmetic|fashion|apparel|winery|\bwine\b|spirits|brewery|snack|footwear|\btoys?\b|\bpet\b"),
    ("Media & Entertainment", 0.8, r"\bmedia\b|entertainment|\bfilm\b|television|\btv\b|\bmusic\b|publishing|streaming|broadcast|\bnews\b|magazine"),
    ("Gaming", 1.2, r"video games?|gaming|esports|game studio"),
    ("Education", 1.0, r"education|universit|\bcollege\b|\bschools?\b|edtech|curriculum"),
    ("Nonprofit", 1.2, r"non-?profit|charit|\bngo\b|advocacy"),
    ("Real Estate", 1.2, r"real estate|proptech|brokerage|\bhousing\b"),
    ("Industrial & Manufacturing", 1.0, r"manufactur|industrial|construction|aerospace|automotive|\benergy\b|logistics|supply chain|utilities"),
    ("Travel & Hospitality", 1.0, r"\btravel|hospitality|\bhotels?\b|airline|tourism|restaurant|resort|cruise"),
    ("Agency & Consulting", 0.8, r"\bagency\b|consultancy|consulting|advertising|design studio|creative studio"),
]
INDUSTRY_RX = [(n, w, re.compile(p, re.I)) for n, w, p in INDUSTRIES]
# Companies on the tracked boards, where the industry is already known.
_BY_CO = {
    "Agency & Consulting": "IDEO|Huge|Code and Theory|R/GA|Landor|Design Bridge|Ogilvy|AKQA|MetaLab|Instrument|Hush|Method",
    "Technology & Software": "Figma|Linear|Notion|Webflow|Vercel|Supabase|PostHog|Sentry|Miro|Zapier|Airtable|Lattice|Descript|Runway|ClickUp|Thumbtack|Asana|Intercom|Datadog|Databricks|Snowflake|MongoDB|Cloudflare|GitLab|Twilio|Dropbox|Samsara|Palantir|Amplitude|Cohere|Perplexity|OpenAI|Anthropic|Gusto|Waymo|Lyft|Nextdoor|Reddit|Discord|Pinterest",
    "Finance & Insurance": "Stripe|Brex|Mercury|Plaid|Chime|Circle|Coinbase|Robinhood|Betterment|SoFi|Wealthfront|Carta|Ramp",
    "Retail & E-commerce": "Poshmark|Instacart|Everlane",
    "Consumer Goods": "Glossier|Away",
    "Healthcare & Wellness": "Calm|WHOOP|Peloton",
    "Media & Entertainment": "Spotify|Substack|Patreon|Medium|Kickstarter|Morning Brew|The Athletic|Hearst",
    "Gaming": "Roblox|Riot Games|Epic Games|Twitch",
    "Education": "Duolingo",
    "Travel & Hospitality": "Airbnb",
    "Industrial & Manufacturing": "Flexport",
}
COMPANY_INDUSTRY = {n.lower(): k for k, v in _BY_CO.items() for n in v.split("|")}


def classify_industry(company, title, desc):
    """Best-guess industry from the company and the posting text, else 'Not stated'."""
    known = COMPANY_INDUSTRY.get((company or "").lower())
    if known:
        return known
    # Benefits text at the end ("health, vision, dental") is not the industry, so the
    # company, title and opening carry the score. Client phrases count anywhere.
    low = f"{company} {title} {desc[:700]}".lower()
    full = f"{company} {title} {desc}".lower()
    # Staffing postings often name the client's field: "our healthcare client".
    hints = " ".join(re.findall(
        r"(?:our|a|an|leading|global)\s+([a-z][a-z &/-]{2,30}?)\s+(?:client|company|brand)\b", full))
    best, best_score = "Not stated", 0
    for name, w, rx in INDUSTRY_RX:
        score = (min(len(rx.findall(low)), 5) + 3 * len(rx.findall(hints))) * w
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 2 else "Not stated"


def classify_type(t, from_text=False):
    """Contract / Part-time / Full-time from a type field, or (stricter) from posting text."""
    if from_text:
        if re.search(r"contract (?:position|role|opportunity|assignment|basis)|"
                     r"(?:contract|temp)[- ]to[- ]hire|temporary (?:position|role|assignment)|"
                     r"\bfreelance\b|\b\d+[- ](?:month|week)s? contract", t):
            return "Contract"
    elif re.search(r"\b(?:contract|contractor|temporary|temp|freelance)\b", t):
        return "Contract"
    if re.search(r"\bpart[- _]?time\b", t):
        return "Part-time"
    if re.search(r"\bfull[- _]?time\b|\bperm(?:anent)?\b", t):
        return "Full-time"
    return ""


def clean_city(c):
    """Drop 'Hybrid - ' style prefixes and cut-off brackets; reject non-cities."""
    c = re.sub(r"^\s*(?:hybrid|on-?site|remote)\s*[-–:]\s*", "", c or "", flags=re.I)
    c = re.sub(r"\s*\([^)]*$", "", c).strip(" -–,")
    return "" if re.search(r"\b(remote|hybrid|anywhere)\b", c, re.I) else c


def normalize(d, source):
    """Turn one raw posting (flat dict) into the shape the web page expects."""
    title = fix_text(pick(d, "title", "jobtitle", "job_title", "positiontitle"))
    url = pick(d, "url", "link", "applyurl", "apply_url", "joburl").strip()
    if not re.match(r"https?://", url, re.I):
        return None
    desc = clean(pick(d, "description", "summary", "body", "jobdescription"))
    raw_city = pick(d, "city")
    city = clean_city(raw_city)
    state = to_state(pick(d, "state", "region", "province", "statecode"))
    loc = pick(d, "location", "joblocation", "locations")
    country = pick(d, "country", "countrycode").strip().lower()

    if loc and not (city and state):
        # One string often lists several offices ("SF, CA • New York, NY") or a
        # country prefix ("US: SF, CA"), so take the first real place.
        first = re.split(r"\s*(?:[•|;]|\bor\b|\band\b|/)\s*", loc)[0]
        first = re.sub(r"^\s*(?:US|USA|U\.S\.)\s*[:\-\u2013]\s*", "", first, flags=re.I)
        m = re.match(r"\s*([^,]+?),\s*([A-Za-z .]+?)(?:\s*,|\s*$)", first)
        if m:
            cand = m.group(1).strip()
            if not re.fullmatch(r"remote|anywhere|flexible|virtual|hybrid|on-?site",
                                cand, re.I):
                city = city or clean_city(cand)
            state = state or to_state(m.group(2))

    place = loc or raw_city
    first_place = re.split(r"\s*(?:[•|;]|\bor\b|\band\b|/)\s*", place)[0]
    nonus = "" if state else detect_country(first_place, country)
    if city and not state and not nonus:
        state = CITY_STATE.get(city.strip().lower(), "")

    field = pick(d, "workplace", "workplacetype", "remotetype", "remote",
                 "locationtype", "jobmode")
    # A board that states the setup outright (Ashby, Lever, Aquent) is trusted
    # before anything guessed from the wording of the posting.
    if re.search(r"hybrid", field, re.I):
        workplace = "hybrid"
    elif re.search(r"remote|telecommut|work from home", field, re.I):
        workplace = "remote"
    elif re.search(r"on[- ]?site|in[- ]person|\boffice\b", field, re.I):
        workplace = "onsite"
    else:
        text = " ".join([field, title, loc, ends(desc, 1500, 800)]).lower()
        if "hybrid" in text:
            workplace = "hybrid"
        elif re.search(r"\bremote\b|work from home|telecommut", text):
            workplace = "remote"
        elif city or state:
            workplace = "onsite"
        else:
            workplace = "unknown"

    # US only: keep US states, explicit US country, or remote with no location.
    # Company boards write the country loosely ("US, Canada", "North America",
    # "US-Remote"), so those count as US too.
    in_us = (bool(state) or country in US_NAMES
             or bool(US_HINT.search(loc)))
    # Remote with no real place named ("Remote; Virtual", "Flexible / Remote").
    open_remote = (workplace == "remote" and not country and not re.sub(
        r"flexible|remote|virtual|anywhere|worldwide|global|work from home|[\W_]+",
        "", loc, flags=re.I))
    if nonus:
        # Keep only remote international roles.
        if workplace != "remote":
            return None
        if source not in NONUS_SOURCES:
            return None
        if scam_signals(f"{title} {desc}") or not looks_english(f"{title} {desc}"):
            return None
    elif not in_us and not open_remote:
        return None
    else:
        nonus = ""

    struct = " ".join([pick(d, "jobtype", "employmenttype", "employment_type",
                            "type", "worktype", "placementtype"),
                       fuzzy(d, r"placement|employment|job_?type|work_?type"),
                       label(desc, "placement type")]).lower()
    jtype = (classify_type(struct) or classify_type(ends(desc, 300, 600).lower(), True)
             or "Unknown")

    job_id = pick(d, "id", "jobid", "referencenumber", "reference", "guid") or url
    start, duration = extract_dates(
        desc,
        label(desc, "start date") or fuzzy(d, r"start"),
        label(desc, "duration") or fuzzy(d, r"duration|length|term"),
        jtype == "Contract")
    company = pick(d, "company", "employer", "companyname") or source
    return {
        "id": f"{source}:{job_id}",
        "source": source,
        "title": title,
        "company": company,
        "url": url,
        "city": city,
        "state": state,
        "country": nonus,
        "workplace": workplace,
        "type": jtype,
        "industry": classify_industry(company, title, desc),
        "posted": to_date(pick(d, "date", "postdate", "posteddate", "posted",
                               "pubdate", "publisheddate", "datecreated",
                               "dateposted", "createddate")),
        "pay": pay_text(d, desc),
        "start": start,
        "duration": duration,
        "tags": extract_tags(title, desc),
        # Keep more of the opening (role & responsibilities) and the tail (pay &
        # requirements) while staying near ~1 MB for jobs.json.
        "description": ends(desc, 1600, 800),
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
    raw = fetch("https://aquent.com/feeds/jobs.xml", timeout=90)
    els = find_jobs(ET.fromstring(raw))
    if not els:
        print(raw[:1500].decode("utf-8", "replace"))
        raise RuntimeError("Could not find job entries in the Aquent feed")
    print("Sample Aquent posting fields:",
          json.dumps({k: v[:120] for k, v in flat(els[0]).items()}, indent=1))
    out = []
    for e in els:
        j = normalize(flat(e), "Aquent")
        if j and j["title"] and AQUENT_TITLE_RE.search(j["title"]) \
                and not AQUENT_SKIP_RE.search(j["title"]):
            out.append(j)
    add_aquent_start_dates(out)
    return out


def add_aquent_start_dates(jobs):
    """The Aquent feed leaves out the Overview box (Start Date, Duration) that
    each posting page shows, so read the start date from the page itself.
    A page that fails or has no date just keeps what the feed gave us."""
    def pull(j):
        try:
            page = clean(fetch(j["url"], timeout=30).decode("utf-8", "replace"))
        except Exception as e:
            print(f"  Aquent page {j['url']} failed: {e}", file=sys.stderr)
            return ""
        start, _ = extract_dates(page)
        return start

    todo = [j for j in jobs if not j["start"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for j, start in zip(todo, pool.map(pull, todo)):
            if start:
                j["start"] = start
    print(f"Aquent start dates: {sum(bool(j['start']) for j in jobs)}/{len(jobs)}")


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


# Each query is paged newest-first until it reaches postings older than
# HIMALAYAS_DAYS (or HIMALAYAS_MAX_PAGES, whichever comes first). A fixed page
# count only ever saw the last few days, so a posting from six weeks ago that is
# still open never made it in. Page size is 20.
HIMALAYAS_DAYS = 75
HIMALAYAS_MAX_PAGES = 25
HIMALAYAS_SEARCHES = [
    "graphic designer", "designer", "ux designer", "ui designer",
    "product designer", "brand designer", "visual designer", "web designer",
    "motion designer", "digital designer", "art director", "creative director",
]


def fetch_himalayas():
    """Himalayas search API for US-eligible design roles.

    Links use the posting's applicationLink unchanged, per their API terms.
    """
    out, seen = [], set()
    dropped = {"title": [], "location": []}
    from urllib.parse import quote_plus
    floor = datetime.now(timezone.utc).timestamp() - HIMALAYAS_DAYS * 86400
    for query in HIMALAYAS_SEARCHES:
        query = quote_plus(query)
        for page in range(1, HIMALAYAS_MAX_PAGES + 1):
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
                    dropped["title"].append(title)
                    continue
                locs = r.get("locationRestrictions") or []
                if locs and not any(looks_us(x) for x in locs):
                    dropped["location"].append(f"{title} {locs}")
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
            stamps = [r["pubDate"] for r in jobs
                      if isinstance(r.get("pubDate"), (int, float))]
            if not jobs or (stamps and max(stamps) < floor):
                break  # out of results, or this whole page is older than we keep
    kept = [j for j in (normalize(d, "Himalayas") for d in out) if j and j["title"]]
    # Shows up in the Actions log so a missing job can be traced to its filter.
    print(f"Himalayas: {len(seen)} seen, {len(dropped['title'])} dropped on title, "
          f"{len(dropped['location'])} on location, {len(out) - len(kept)} in normalize, "
          f"{len(kept)} kept")
    for why, rows in dropped.items():
        for t in rows[:60]:
            print(f"  Himalayas dropped ({why}): {t}")
    return kept


# ---------------------------------------------------------------------------
# Company job boards. Greenhouse, Ashby and Lever all publish a company's open
# roles as public JSON -- no key, no quota, no signup -- and this is where
# on-site and hybrid design work actually lives; the remote feeds above almost
# never carry it. A board that moves or closes returns 404 and simply adds
# nothing, so this list is safe to edit by hand.
# (slug, display name, platform)
ATS_BOARDS = [
    # studios and agencies
    ("ideo", "IDEO", "greenhouse"),
    ("hugeinc", "Huge", "greenhouse"),
    ("codeandtheory", "Code and Theory", "greenhouse"),
    ("rga", "R/GA", "greenhouse"),
    ("landor", "Landor", "greenhouse"),
    ("designbridge", "Design Bridge", "greenhouse"),
    ("ogilvy", "Ogilvy", "greenhouse"),
    ("akqa", "AKQA", "greenhouse"),
    ("metalab", "MetaLab", "greenhouse"),
    ("instrument", "Instrument", "lever"),
    ("hush", "Hush", "lever"),
    ("method", "Method", "greenhouse"),
    # design-led product and tech
    ("figma", "Figma", "greenhouse"),
    ("linear", "Linear", "ashby"),
    ("notion", "Notion", "ashby"),
    ("webflow", "Webflow", "ashby"),
    ("vercel", "Vercel", "greenhouse"),
    ("supabase", "Supabase", "ashby"),
    ("posthog", "PostHog", "ashby"),
    ("sentry", "Sentry", "ashby"),
    ("miro", "Miro", "ashby"),
    ("zapier", "Zapier", "ashby"),
    ("airtable", "Airtable", "greenhouse"),
    ("carta", "Carta", "greenhouse"),
    ("ramp", "Ramp", "greenhouse"),
    ("lattice", "Lattice", "greenhouse"),
    ("descript", "Descript", "greenhouse"),
    ("runway", "Runway", "ashby"),
    ("clickup", "ClickUp", "ashby"),
    ("thumbtack", "Thumbtack", "ashby"),
    ("stripe", "Stripe", "greenhouse"),
    ("brex", "Brex", "greenhouse"),
    ("mercury", "Mercury", "greenhouse"),
    ("plaid", "Plaid", "ashby"),
    ("chime", "Chime", "greenhouse"),
    ("circle", "Circle", "ashby"),
    ("coinbase", "Coinbase", "greenhouse"),
    ("robinhood", "Robinhood", "greenhouse"),
    ("betterment", "Betterment", "greenhouse"),
    ("sofi", "SoFi", "greenhouse"),
    ("wealthfront", "Wealthfront", "lever"),
    ("gusto", "Gusto", "greenhouse"),
    ("asana", "Asana", "greenhouse"),
    ("intercom", "Intercom", "greenhouse"),
    ("datadog", "Datadog", "greenhouse"),
    ("databricks", "Databricks", "greenhouse"),
    ("snowflake", "Snowflake", "ashby"),
    ("mongodb", "MongoDB", "greenhouse"),
    ("cloudflare", "Cloudflare", "greenhouse"),
    ("gitlab", "GitLab", "greenhouse"),
    ("twilio", "Twilio", "greenhouse"),
    ("dropbox", "Dropbox", "greenhouse"),
    ("samsara", "Samsara", "greenhouse"),
    ("flexport", "Flexport", "greenhouse"),
    ("palantir", "Palantir", "lever"),
    ("amplitude", "Amplitude", "ashby"),
    ("cohere", "Cohere", "ashby"),
    ("perplexity", "Perplexity", "ashby"),
    ("openai", "OpenAI", "ashby"),
    ("anthropic", "Anthropic", "greenhouse"),
    ("duolingo", "Duolingo", "greenhouse"),
    ("pinterest", "Pinterest", "greenhouse"),
    ("reddit", "Reddit", "greenhouse"),
    ("discord", "Discord", "greenhouse"),
    ("twitch", "Twitch", "greenhouse"),
    ("roblox", "Roblox", "greenhouse"),
    ("riotgames", "Riot Games", "greenhouse"),
    ("epicgames", "Epic Games", "greenhouse"),
    ("spotify", "Spotify", "lever"),
    ("airbnb", "Airbnb", "greenhouse"),
    ("lyft", "Lyft", "greenhouse"),
    ("instacart", "Instacart", "greenhouse"),
    ("waymo", "Waymo", "greenhouse"),
    ("peloton", "Peloton", "greenhouse"),
    ("calm", "Calm", "greenhouse"),
    ("whoop", "WHOOP", "ashby"),
    ("glossier", "Glossier", "greenhouse"),
    ("everlane", "Everlane", "greenhouse"),
    ("away", "Away", "ashby"),
    ("poshmark", "Poshmark", "ashby"),
    ("substack", "Substack", "ashby"),
    ("patreon", "Patreon", "ashby"),
    ("medium", "Medium", "greenhouse"),
    ("kickstarter", "Kickstarter", "greenhouse"),
    ("morningbrew", "Morning Brew", "lever"),
    ("theathletic", "The Athletic", "lever"),
    ("hearst", "Hearst", "greenhouse"),
    ("nextdoor", "Nextdoor", "greenhouse"),
]

# Titles worth keeping from a company board. Wider than DESIGN_TITLE_RE because
# agencies name roles "Creative Director" or "Art Director" and never say
# "designer".
ATS_TITLE_RE = re.compile(
    r"\bdesign(?:er|ers|ing)?\b|\bux\b|\bui\b|graphic|visual|illustrat|brand|packag|"
    r"\bmotion\b|animat|art director|creative|typograph|retouch|video edit|"
    r"concept art|storyboard|\b3d\b|wayfinding|signage|experiential|exhibit|"
    r"environmental graphic|\bartist\b",
    re.I,
)
# Titles that only look like design work: chip and hardware design, game design,
# engineering, marketing and comms, L&D, ops, and university faculty posts.
ATS_SKIP_RE = re.compile(
    r"engineer|engineering|developer|devops|programmer|software|architect|technician|"
    r"scientist|analyst|coordinator|"
    r"\bgame design|\bgames creative|\blevel design|\bsystems design|\beconomy design|"
    r"\bnarrative design|\bquest design|\bcombat design|\btechnical design|"
    r"\bsound design|\bcapture artist|"
    r"program manager|project manager|product manager|"
    r"marketing creative|creative services|creative strateg|"
    r"brand marketing|brand consultant|brand partnerships|brand manager|talent brand|"
    r"brand\s*(?:&|and)?\s*communications|brand\s*(?:&|and)?\s*content|branded|"
    r"social media manager|community manager|account manager|operations manager|"
    r"people partner|recruit|enablement|policy design|"
    r"learning design|learning experience|instructional design|"
    r"workplace|sponsorship|pricing and packaging|packaging assistant|"
    r"packaging department|mailroom|visuals internship|\bbim\b|"
    r"verification|semiconductor|silicon|\bchip\b|firmware|hardware|\basic\b|\bfpga\b|"
    r"professor|faculty|\bdean\b|lecturer|instructor|tenure|postdoc|adjunct|"
    r"head of the department|chair of|copywriter|translator|interpreter|"
    r"\beditor\b|editorial director",
    re.I,
)

# Open feeds use the company-board skip list, minus "editor": a video editor is a Motion role.
OPEN_SKIP_RE = re.compile(
    ATS_SKIP_RE.pattern.replace(r"\beditor\b|editorial director", "editorial director"), re.I)


def _greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    rows = []
    for j in data.get("jobs") or []:
        meta = " ".join(f"{m.get('name', '')}: {m.get('value', '')}"
                        for m in (j.get("metadata") or []) if isinstance(m, dict))
        rows.append({"id": str(j.get("id") or j.get("absolute_url")),
                     "title": j.get("title", ""),
                     "url": j.get("absolute_url", ""),
                     "location": (j.get("location") or {}).get("name", ""),
                     "description": j.get("content", ""),
                     "date": j.get("first_published") or j.get("updated_at") or "",
                     "workplace": meta})
    return rows


def _ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    rows = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        places = [j.get("location") or ""]
        places += [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        rows.append({"id": str(j.get("id") or j.get("jobUrl")),
                     "title": j.get("title", ""),
                     "url": j.get("jobUrl", ""),
                     "location": " • ".join(p for p in places if p),
                     "description": j.get("descriptionPlain", ""),
                     "date": j.get("publishedAt", ""),
                     "workplace": j.get("workplaceType", ""),
                     "jobtype": j.get("employmentType", "")})
    return rows


def _lever(slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    rows = []
    for j in data or []:
        cat = j.get("categories") or {}
        created = j.get("createdAt")
        rows.append({"id": str(j.get("id") or j.get("hostedUrl")),
                     "title": j.get("text", ""),
                     "url": j.get("hostedUrl", ""),
                     "location": cat.get("location", ""),
                     "description": j.get("descriptionPlain", ""),
                     "date": (datetime.fromtimestamp(int(created) / 1000, timezone.utc)
                              .date().isoformat() if created else ""),
                     "workplace": j.get("workplaceType", ""),
                     "jobtype": cat.get("commitment", "")})
    return rows


_ATS = {"greenhouse": _greenhouse, "ashby": _ashby, "lever": _lever}


def fetch_company_boards():
    """Every studio, agency and design-led company we track, as one source."""
    out, ok, failed = [], 0, 0

    def pull(board):
        try:
            return _ATS[board[2]](board[0]), None
        except Exception as e:  # a board that moved must not sink the source
            return None, e

    # The boards sit on different hosts, so a few at a time is polite and much faster.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pulled = list(pool.map(pull, ATS_BOARDS))
    for (slug, name, platform), (rows, err) in zip(ATS_BOARDS, pulled):
        if err:
            failed += 1
            print(f"  board {name} failed: {err}", file=sys.stderr)
            continue
        ok += 1
        kept = 0
        for d in rows:
            title = d["title"]
            if not title or not ATS_TITLE_RE.search(title) or ATS_SKIP_RE.search(title):
                continue
            d["id"] = f"{slug}:{d['id']}"
            d["company"] = name
            j = normalize(d, "Company boards")
            if j and j["title"]:
                out.append(j)
                kept += 1
        if kept:
            print(f"  {name:16} {platform:11} {kept} design roles")
    print(f"Company boards: {ok}/{len(ATS_BOARDS)} responded, {failed} failed, "
          f"{len(out)} design roles")
    if not ok:
        raise RuntimeError("no company board responded")
    return out


COROFLOT_SEEKING = re.compile(r"^(.*?)\s+is seeking\s+(?:an?|the)?\s*(.+)$", re.I)


def fetch_coroflot():
    """Coroflot's creative job feed: the 30 newest, mostly on-site studio work."""
    root = ET.fromstring(fetch("https://www.coroflot.com/jobs/rss"))
    rows = []
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        d = {local(c.tag): (c.text or "") for c in item}
        title, company = d.get("title", "").strip(), ""
        m = COROFLOT_SEEKING.match(title)
        if m:  # "Cass Calder Smith ... is seeking an Interior Designer"
            company, title = m.group(1).strip(), m.group(2).strip()
        rows.append({"id": d.get("guid") or d.get("link") or title,
                     "title": title, "company": company,
                     "url": d.get("link", ""),
                     "location": d.get("description", "").strip(),
                     "date": d.get("pubdate", ""),
                     "description": ""})
    kept = [d for d in rows
            if d["title"] and DESIGN_TITLE_RE.search(d["title"])
            and not ATS_SKIP_RE.search(d["title"])]
    return [j for j in (normalize(d, "Coroflot") for d in kept) if j and j["title"]]


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
    "Company boards": fetch_company_boards,
    "Coroflot": fetch_coroflot,
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
                got = [j for j in got if AQUENT_TITLE_RE.search(j["title"])
                       and not AQUENT_SKIP_RE.search(j["title"])]
            elif name == "Company boards":
                got = [j for j in got if ATS_TITLE_RE.search(j["title"])
                       and not ATS_SKIP_RE.search(j["title"])]
        got = [j for j in got if title_ok(name, j["title"])]
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

    # An aggregator (Adzuna) can re-list a job a company board already carries.
    # Sources are ordered richest first above, so the first copy wins.
    seen, deduped = set(), {}
    for jid, j in jobs.items():
        key = (j["company"].lower(), j["title"].lower(), j["city"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped[jid] = j
    jobs = deduped

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