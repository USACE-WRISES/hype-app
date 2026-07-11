"""Canonical-hashing + staleness tests (spec §13.1, §4.3)."""
import pytest

from hype_app.hashing import (
    INPUT_GROUPS,
    ResultStatus,
    changed_groups,
    group_hashes,
    result_status,
    stable_hash,
)


class TestStableHash:
    def test_key_order_independent(self):
        assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})

    def test_nested_order_independent(self):
        a = {"x": [1, 2, 3], "y": {"p": 1, "q": 2}}
        b = {"y": {"q": 2, "p": 1}, "x": [1, 2, 3]}
        assert stable_hash(a) == stable_hash(b)

    def test_float_format_noise_folds(self):
        # 0.1 + 0.2 == 0.30000000000000004; rounding to 12 sig figs makes it equal 0.3.
        assert stable_hash({"v": 0.1 + 0.2}) == stable_hash({"v": 0.3})

    def test_negative_zero_folds(self):
        assert stable_hash({"v": -0.0}) == stable_hash({"v": 0.0})

    def test_list_order_matters(self):
        assert stable_hash([1, 2, 3]) != stable_hash([3, 2, 1])

    def test_nan_is_hashable_and_stable(self):
        h1 = stable_hash({"v": float("nan")})
        h2 = stable_hash({"v": float("nan")})
        assert h1 == h2  # normalized to the string "NaN"

    def test_digest_is_hex_sha256(self):
        d = stable_hash({"a": 1})
        assert len(d) == 64 and all(c in "0123456789abcdef" for c in d)


class TestGroupHashes:
    def test_valid_groups(self):
        gh = group_hashes({"geometry": {"reach": 1}, "terrain": {"dem": "x"}})
        assert set(gh) == {"geometry", "terrain"}
        assert all(len(v) == 64 for v in gh.values())

    def test_unknown_group_raises(self):
        with pytest.raises(ValueError):
            group_hashes({"not_a_group": 1})

    def test_all_groups_known(self):
        assert group_hashes({g: g for g in INPUT_GROUPS}).keys() == set(INPUT_GROUPS)


class TestStaleness:
    def test_missing_when_no_record(self):
        assert result_status({"geometry": "h"}, None) is ResultStatus.missing
        assert result_status({"geometry": "h"}, {}) is ResultStatus.missing

    def test_current_when_all_match(self):
        cur = {"geometry": "h1", "grid": "h2"}
        assert result_status(cur, dict(cur)) is ResultStatus.current

    def test_stale_when_a_group_changes(self):
        rec = {"geometry": "h1", "grid": "h2"}
        cur = {"geometry": "h1", "grid": "CHANGED"}
        assert result_status(cur, rec) is ResultStatus.stale
        assert changed_groups(cur, rec) == ["grid"]

    def test_failed_overrides(self):
        cur = {"geometry": "h1"}
        assert result_status(cur, dict(cur), failed=True) is ResultStatus.failed

    def test_changed_groups_handles_missing_keys(self):
        assert changed_groups({"a": "1", "b": "2"}, {"a": "1"}) == ["b"]
