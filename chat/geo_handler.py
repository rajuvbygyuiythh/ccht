"""Country / geo detection and location-response rotation.

Flexible geo: bot can be from ANY country. Per session, it either:
  1. Matches the user's country (if user shared it), OR
  2. Picks a random country for variety.

Bot NEVER contradicts user's country — always builds rapport.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Country-region mapping (group -> list of countries)
# ---------------------------------------------------------------------------

REGION_MAP: Dict[str, List[str]] = {
    "north_america": ["usa", "united states", "us", "canada", "ca", "mexico"],
    "europe": [
        "uk", "united kingdom", "england", "scotland", "wales",
        "germany", "france", "spain", "italy", "netherlands",
        "belgium", "portugal", "sweden", "norway", "denmark",
        "finland", "switzerland", "austria", "ireland", "poland",
        "czech republic", "czechia", "romania", "hungary", "greece",
    ],
    "oceania": ["australia", "au", "new zealand", "nz"],
    "asia_pacific": [
        "japan", "south korea", "korea", "hong kong", "singapore",
        "taiwan", "philippines", "india", "pakistan", "bangladesh",
        "sri lanka", "nepal", "malaysia", "thailand", "vietnam",
        "indonesia",
    ],
    "middle_east": [
        "israel", "uae", "united arab emirates", "dubai",
        "saudi arabia", "qatar", "bahrain", "kuwait", "oman",
    ],
    "latin_america": [
        "brazil", "argentina", "colombia", "chile", "peru",
        "uruguay", "panama", "puerto rico", "venezuela", "ecuador",
    ],
    "africa": [
        "nigeria", "south africa", "kenya", "ghana", "egypt",
        "morocco", "tanzania", "ethiopia", "uganda",
    ],
}

# Bot country pool (random each session, unique names)
_BOT_COUNTRY_POOL = {
    "uk": ["uk", "england", "britain", "the uk", "uk england"],
    "germany": ["germany", "deutschland", "germany europe", "de"],
    "canada": ["canada", "ca", "canadian", "the north"],
    "australia": ["australia", "au", "down under", "aussie land"],
    "france": ["france", "fr", "french land", "the france"],
    "spain": ["spain", "es", "spanish land", "the spain"],
    "italy": ["italy", "it", "italian land", "the italy"],
    "netherlands": ["netherlands", "holland", "nl", "the dutch"],
    "sweden": ["sweden", "se", "swedish land", "the sweden"],
    "norway": ["norway", "no", "norwegian", "the norway"],
    "japan": ["japan", "jp", "japanese land", "the japan"],
    "singapore": ["singapore", "sg", "lion city", "the singapore"],
    "brazil": ["brazil", "br", "brasil", "the brazil"],
    "mexico": ["mexico", "mx", "mexican land", "the mexico"],
}

# Country cities mapping
_COUNTRY_CITIES = {
    "uk": ["london", "manchester", "birmingham", "liverpool", "edinburgh", "bristol"],
    "germany": ["berlin", "munich", "hamburg", "frankfurt", "cologne", "stuttgart"],
    "canada": ["toronto", "vancouver", "montreal", "calgary", "ottawa", "edmonton"],
    "australia": ["sydney", "melbourne", "brisbane", "perth", "adelaide", "gold coast"],
    "france": ["paris", "lyon", "marseille", "nice", "bordeaux", "toulouse"],
    "spain": ["madrid", "barcelona", "valencia", "seville", "malaga", "bilbao"],
    "italy": ["rome", "milan", "naples", "turin", "florence", "venice"],
    "netherlands": ["amsterdam", "rotterdam", "the hague", "utrecht", "eindhoven"],
    "sweden": ["stockholm", "gothenburg", "malmo", "uppsala", "linkoping"],
    "norway": ["oslo", "bergen", "trondheim", "stavanger", "troms"],
    "japan": ["tokyo", "osaka", "kyoto", "yokohama", "nagoya", "sapporo"],
    "singapore": ["singapore"],
    "brazil": ["sao paulo", "rio de janeiro", "brasilia", "salvador", "fortaleza"],
    "mexico": ["mexico city", "cancun", "guadalajara", "monterrey", "puebla"],
}

# High-priority regions for snap redirect
HIGH_PRIORITY_COUNTRIES = {
    "usa", "united states", "us", "canada", "ca",
    "australia", "au", "new zealand", "nz",
    "uk", "united kingdom", "england", "scotland", "wales",
    "germany", "france", "spain", "italy", "netherlands",
    "belgium", "portugal", "sweden", "norway", "denmark",
    "finland", "switzerland", "austria", "ireland", "poland",
    "czech republic", "czechia", "romania", "hungary", "greece",
    "israel", "south korea", "hong kong", "singapore",
    "japan", "taiwan", "uae",
    "mexico", "chile", "uruguay", "panama", "puerto rico",
}


def _detect_region(country_input: str) -> Optional[str]:
    """Map a free-text country string to a region key."""
    lower = country_input.lower().strip()
    for region, countries in REGION_MAP.items():
        for c in countries:
            if c in lower or lower in c:
                return region
    return None


def is_high_priority(country_input: str) -> bool:
    """Return True if the country qualifies for the full redirect flow."""
    lower = country_input.lower().strip()
    return any(
        hp in lower or lower in hp for hp in HIGH_PRIORITY_COUNTRIES
    )


def get_location_response(user_country: Optional[str], bot_country: str) -> str:
    """Return a natural location reply with COUNTRY ONLY (no city)."""
    if user_country:
        return random.choice([
            f"{user_country} nice",
            f"same! {user_country} cool",
            f"oh {user_country}! i like that",
            f"no way {user_country}",
        ])
    else:
        # Use bot's random country name (prefer full name, avoid confusing abbreviations)
        country_names = _BOT_COUNTRY_POOL.get(bot_country, [bot_country])
        safe_names = [n for n in country_names if len(n) > 2]
        country_name = random.choice(safe_names if safe_names else country_names)
        return random.choice([
            f"{country_name} u?",
            f"{country_name} wbu?",
            f"im {country_name} hbu?",
            f"{country_name} lol wbu",
        ])


class CountryRotator:
    """Maintains a per-session country assignment for EVA.

    Bot picks ONE random country per session (never same twice).
    If the user reveals their country, the bot mirrors it.
    """

    def __init__(self) -> None:
        self._assigned_country: str = random.choice(list(_BOT_COUNTRY_POOL.keys()))
        self._user_country: Optional[str] = None
        self._matched_user: bool = False

    @property
    def current_country(self) -> str:
        return self._assigned_country

    def set_user_country(self, country: str) -> None:
        """User revealed their country — bot mirrors it."""
        self._user_country = country
        self._matched_user = True

    @property
    def user_country(self) -> Optional[str]:
        return self._user_country

    def user_is_high_priority(self) -> bool:
        if self._user_country is None:
            return False
        return is_high_priority(self._user_country)

    def respond_to_location_question(self) -> str:
        """Generate EVA's reply when the user asks 'where are you from?'."""
        return get_location_response(self._user_country, self.current_country)
