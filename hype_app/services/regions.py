"""StreamStats/NSS region choices for the flow-review modal.

A hardcoded state/territory list — deliberately NOT a spatial lookup: shapely and bundled
boundary data stay out of the main Shiny process (geometry-heavy work runs in spawn children).
Auto-detection of a blank region happens in the lookup child via the FCC area API
(hype_app/usgs_run.py -> streamstats.suggest_region)."""
from __future__ import annotations

STREAMSTATS_REGIONS: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
}


def region_choices() -> dict[str, str]:
    """{code: "XX — Name"} for a select input (the UI prepends its own Auto-detect entry)."""
    return {c: f"{c} — {n}" for c, n in STREAMSTATS_REGIONS.items()}


__all__ = ["STREAMSTATS_REGIONS", "region_choices"]
