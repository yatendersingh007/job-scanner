"""Geography + recency filters.

Multi-region: a posting's free-text location is mapped to exactly one of
US | UK | Canada | India | Unknown. The pipeline keeps a posting when its
region is in the configured `regions` list (or the location is blank and
empty_passes is on). The detected region is also written to the CSV so you
can slice by market in Excel or the app.

Order matters in region_of(): the most specific / least ambiguous signals
are checked first, and a few tokens that collide across countries (e.g.
"IN" the US-state abbreviation for Indiana vs the ISO code for India, or
"CA" California vs Canada) are handled deliberately rather than by raw
token overlap.
"""
from __future__ import annotations

import re
from datetime import date

# ── United States ─────────────────────────────────────────────────────────
_US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
}
_US_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}
_US_KEYWORDS = ("united states", "usa", "u.s.", "us-", "remote - us", "remote, us",
                "remote (us", "remote us")

# ── United Kingdom ──────────────────────────────────────────────────────────
_UK_KEYWORDS = (
    "united kingdom", "uk", "u.k.", "england", "scotland", "wales",
    "northern ireland", "great britain", "britain",
    "london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
    "liverpool", "bristol", "sheffield", "cardiff", "belfast", "nottingham",
    "newcastle", "reading", "cambridge", "oxford", "brighton", "leicester",
    "canary wharf", "city of london",
)

# ── Canada ──────────────────────────────────────────────────────────────────
_CA_PROVINCES = {
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "newfoundland", "nova scotia", "ontario",
    "prince edward island", "quebec", "québec", "saskatchewan",
    "northwest territories", "nunavut", "yukon",
}
_CA_ABBR = {"ab", "bc", "mb", "nb", "nl", "ns", "on", "pe", "qc", "sk",
            "nt", "nu", "yt"}
_CA_KEYWORDS = (
    "canada", "toronto", "vancouver", "montreal", "montréal", "calgary",
    "ottawa", "edmonton", "winnipeg", "mississauga", "halifax", "hamilton",
    "kitchener", "waterloo", "victoria", "quebec city", "burnaby", "markham",
)

# ── India ───────────────────────────────────────────────────────────────────
_IN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "delhi",
}
_IN_KEYWORDS = (
    "india", "mumbai", "delhi", "new delhi", "bangalore", "bengaluru",
    "hyderabad", "chennai", "kolkata", "pune", "ahmedabad", "gurgaon",
    "gurugram", "noida", "jaipur", "chandigarh", "kochi", "coimbatore",
    "thiruvananthapuram", "indore", "nagpur", "ncr",
)

# ── Mexico (detection built in; enable by adding "Mexico" to config regions) ─
# Deliberately CONSERVATIVE: only the country word and unambiguous major
# cities. Mexican STATE names are intentionally omitted because many collide
# with US places (Durango CO, Monterey CA, etc.) — matching them would wrongly
# tag US jobs as Mexico and, while Mexico is disabled, silently reject them.
# "New Mexico" (a US state) is explicitly guarded against in region_of().
_MX_KEYWORDS = (
    "mexico", "méxico", "cdmx", "mexico city", "ciudad de mexico",
    "guadalajara", "monterrey", "tijuana", "cancun", "cancún",
    "remote - mexico", "remote mexico",
)

REGIONS = ("US", "UK", "Canada", "India", "Mexico")


def _tokens(location: str) -> set[str]:
    return {t.strip(",.()").lower() for t in location.replace("/", " ").split()}


def _hit(loc: str, phrases) -> bool:
    """True if any phrase appears in loc as a whole word/phrase. Word
    boundaries stop false collisions like "india" matching "indiana" or
    "uk" matching "ukraine". Phrases containing punctuation (e.g. "u.s.",
    "remote - us") fall back to plain substring, where boundaries misbehave."""
    for p in phrases:
        if p.replace(" ", "").isalnum():
            if re.search(r"\b" + re.escape(p) + r"\b", loc):
                return True
        elif p in loc:
            return True
    return False


def region_of(location: str) -> str:
    """Map a free-text location to US | UK | Canada | India | Unknown.

    Whole-word phrase/city hits are checked before bare 2-letter
    abbreviations, because abbreviations collide (CA = California AND Canada;
    IN = Indiana AND India). When only an ambiguous abbreviation is present
    we prefer the US reading (the historical default market) unless a clearer
    non-US signal is also present."""
    if not location:
        return "Unknown"
    loc = location.lower()
    toks = _tokens(location)

    # 1. Unambiguous country / region words and city names first.
    if _hit(loc, _IN_KEYWORDS) or _hit(loc, _IN_STATES):
        return "India"
    # Mexico, but never "New Mexico" (that's a US state).
    if _hit(loc, _MX_KEYWORDS) and not _hit(loc, ("new mexico",)):
        return "Mexico"
    if _hit(loc, _CA_KEYWORDS) or _hit(loc, _CA_PROVINCES):
        return "Canada"
    if _hit(loc, _UK_KEYWORDS):
        return "UK"
    if _hit(loc, _US_KEYWORDS) or _hit(loc, _US_STATES):
        return "US"

    # 2. Bare province/state abbreviations (only if a country wasn't named).
    if toks & _CA_ABBR:
        return "Canada"
    if toks & _US_ABBR:
        return "US"
    return "Unknown"


def location_matches(location: str, regions: list[str], *, empty_passes: bool = True) -> bool:
    """Keep this posting? True if its detected region is one we want, or the
    location is blank and empty_passes is on (blank = Unknown, surfaced now,
    filtered later once a real location is known)."""
    if not location:
        return empty_passes
    return region_of(location) in set(regions)


# Backwards-compatible helper (older callers / tests may still use it).
def is_us_location(location: str, *, empty_passes: bool = True) -> bool:
    if not location:
        return empty_passes
    return region_of(location) == "US"


def within_recency(posted_or_seen: date | None, today: date, max_days: int) -> bool:
    if posted_or_seen is None:
        return False
    return (today - posted_or_seen).days <= max_days
