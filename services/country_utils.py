"""Utilities for normalising country data from the WarEra API."""

# Static list of all 173 countries in WarEra (used for autocomplete).
ALL_COUNTRY_NAMES: list[str] = [
    "Afghanistan",
    "Albania",
    "Algeria",
    "Angola",
    "Argentina",
    "Armenia",
    "Australia",
    "Austria",
    "Azerbaijan",
    "Bahamas",
    "Bangladesh",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bhutan",
    "Bolivia",
    "Bosnia",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burundi",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Cape Verde",
    "Central Africa",
    "Chad",
    "Chile",
    "China",
    "Colombia",
    "Congo",
    "Costa Rica",
    "Croatia",
    "Cuba",
    "Cyprus",
    "Czechia",
    "Denmark",
    "Djibouti",
    "Dominican Republic",
    "DR Congo",
    "East Timor",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Equatorial Guinea",
    "Eritrea",
    "Estonia",
    "Eswatini",
    "Ethiopia",
    "Fiji",
    "Finland",
    "France",
    "Gabon",
    "Gambia",
    "Georgia",
    "Germany",
    "Ghana",
    "Greece",
    "Greenland",
    "Guatemala",
    "Guinea",
    "Guinea-Bissau",
    "Guyana",
    "Haiti",
    "Honduras",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Ireland",
    "Israel",
    "Italy",
    "Ivory Coast",
    "Jamaica",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kosovo",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Latvia",
    "Lebanon",
    "Lesotho",
    "Liberia",
    "Libya",
    "Lithuania",
    "Luxembourg",
    "Madagascar",
    "Malawi",
    "Malaysia",
    "Mali",
    "Malta",
    "Mauritania",
    "Mexico",
    "Moldova",
    "Mongolia",
    "Montenegro",
    "Morocco",
    "Mozambique",
    "Myanmar",
    "Namibia",
    "Nepal",
    "Netherlands",
    "New Zealand",
    "Nicaragua",
    "Niger",
    "Nigeria",
    "North Korea",
    "North Macedonia",
    "Norway",
    "Oman",
    "Pakistan",
    "Palestine",
    "Panama",
    "Papua New Guinea",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Puerto Rico",
    "Qatar",
    "Romania",
    "Russia",
    "Rwanda",
    "Saudi Arabia",
    "Senegal",
    "Serbia",
    "Sierra Leone",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "Solomon Islands",
    "Somalia",
    "South Africa",
    "South Korea",
    "South Sudan",
    "Spain",
    "Sri Lanka",
    "Sudan",
    "Suriname",
    "Sweden",
    "Switzerland",
    "Syria",
    "São Tomé and Príncipe",
    "Taiwan",
    "Tajikistan",
    "Tanzania",
    "Thailand",
    "Togo",
    "Trinidad and Tobago",
    "Tunisia",
    "Turkey",
    "Turkmenistan",
    "Uganda",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "Uruguay",
    "Uzbekistan",
    "Vanuatu",
    "Venezuela",
    "Vietnam",
    "Yemen",
    "Zambia",
    "Zimbabwe",
]


def extract_country_list(api_response) -> list[dict]:
    """Normalise the getAllCountries API envelope into a plain list of country dicts."""
    if isinstance(api_response, list):
        return [c for c in api_response if isinstance(c, dict)]
    if isinstance(api_response, dict):
        if isinstance(api_response.get("data"), list):
            return [c for c in api_response["data"] if isinstance(c, dict)]
        r = api_response.get("result")
        if isinstance(r, dict) and isinstance(r.get("data"), list):
            return [c for c in r["data"] if isinstance(c, dict)]
        for key in ("countries", "items"):
            v = api_response.get(key)
            if isinstance(v, list):
                return [c for c in v if isinstance(c, dict)]
    return []


_COUNTRY_ALIASES: dict[str, str] = {
    "turkey": "turkiye",
}


def find_country(query: str, country_list: list[dict]) -> dict | None:
    """Find a country by code or name (case-insensitive).

    Matching priority:
      1. Exact code match  (e.g. "NL", "CH")
      2. Exact name match  (e.g. "Netherlands", "Switzerland")
      3. Name starts-with  (e.g. "switz" → Switzerland)
    """
    q = _COUNTRY_ALIASES.get(query.strip().lower(), query.strip().lower())
    hit = next((c for c in country_list if str(c.get("code", "")).lower() == q), None)
    if hit:
        return hit
    hit = next((c for c in country_list if str(c.get("name", "")).lower() == q), None)
    if hit:
        return hit
    return next(
        (c for c in country_list if str(c.get("name", "")).lower().startswith(q)), None
    )


def country_id(country: dict) -> str:
    """Return the best available ID for a country dict."""
    return str(
        country.get("_id")
        or country.get("id")
        or country.get("countryId")
        or country.get("code")
    )
