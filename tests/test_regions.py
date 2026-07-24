"""Region-choice list for the USGS flow-review modal (hype_app.services.regions)."""
from hype_app.services.regions import STREAMSTATS_REGIONS, region_choices


def test_region_choices_shape():
    ch = region_choices()
    assert len(ch) >= 55                       # 50 states + DC + territories
    assert "" not in ch                        # the UI prepends its own Auto-detect entry
    assert all(len(k) == 2 and k.isalpha() and k.isupper() for k in ch)
    assert ch["NH"] == "NH — New Hampshire"


def test_region_choices_labels_match_source():
    ch = region_choices()
    assert set(ch) == set(STREAMSTATS_REGIONS)
    assert all(ch[k] == f"{k} — {STREAMSTATS_REGIONS[k]}" for k in ch)
