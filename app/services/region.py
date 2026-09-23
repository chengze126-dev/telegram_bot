"""Map an *explicit* location string exposed by Telegram to a coarse region.

This only parses location text that Telegram itself exposes (e.g. a Telegram
Business location). It never infers location from names, language, avatars
or phone numbers. Anything that is not an explicit match stays "Unknown"
(no data) or "Other" (explicit location outside the US/EU lists).
"""

from __future__ import annotations

import re

US_TERMS = {
    "united states", "usa", "u.s.a.", "u.s.", "us", "america",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
    "san francisco", "los angeles", "seattle", "chicago", "boston", "austin", "miami", "nyc",
}

EU_TERMS = {
    "european union", "eu", "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "czech republic", "denmark", "estonia", "finland", "france", "germany", "deutschland", "greece",
    "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands",
    "holland", "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
    "berlin", "paris", "amsterdam", "madrid", "barcelona", "lisbon", "rome", "milan", "vienna",
    "warsaw", "prague", "dublin", "stockholm", "copenhagen", "helsinki", "brussels", "munich",
    "tallinn", "riga", "vilnius", "athens", "budapest", "bucharest", "sofia", "zagreb",
}

_TOKEN_SPLIT = re.compile(r"[,;/|()\n]+")


def region_from_explicit_location(location: str | None) -> str | None:
    """Return "US", "EU" or "Other" for an explicit location, or None when absent."""
    if not location or not location.strip():
        return None
    text = location.lower()
    parts = [p.strip(" .") for p in _TOKEN_SPLIT.split(text) if p.strip(" .")]
    candidates = set(parts)
    for part in parts:
        words = part.split()
        candidates.update(words)
        candidates.update(" ".join(words[i:i + 2]) for i in range(len(words) - 1))
        candidates.update(" ".join(words[i:i + 3]) for i in range(len(words) - 2))
    if candidates & US_TERMS:
        return "US"
    if candidates & EU_TERMS:
        return "EU"
    return "Other"
