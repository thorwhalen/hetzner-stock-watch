"""Tests for the Hetzner stock checker.

Stdlib ``unittest`` only, so CI needs no dependencies. The fixture in
``fixture_server_types.json`` mirrors the shape of a real ``GET /v1/server_types``
response, including the edge cases that decide whether an alert fires: a sold-out
location, a location whose ``deprecation.unavailable_after`` has passed, one that is
deprecated but still buyable, and one with no ``available`` key at all.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from check_hetzner import (  # noqa: E402
    Config,
    HetznerWatchError,
    check,
    iter_matching_offers,
    load_seen,
    save_seen,
)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture_server_types.json")
NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _server_types():
    with open(FIXTURE, encoding="utf-8") as stream:
        return json.load(stream)["server_types"]


def _cfg(**kwargs):
    defaults = dict(token="test", enrich_locations=False, state_file="/dev/null")
    return Config(**{**defaults, **kwargs})


def _keys(cfg):
    return {o.key for o in iter_matching_offers(_server_types(), cfg, now=NOW)}


class TestMatching(unittest.TestCase):
    def test_default_spec_finds_only_in_stock_32gb_shared(self):
        # cx52@nbg1 (in stock), cax41@fsn1 (in stock), cpx51@ash (in stock),
        # cax51@fsn1 (64 GB, no `available` key -> assumed available).
        self.assertEqual(
            _keys(_cfg()),
            {"cx52@nbg1", "cax41@fsn1", "cpx51@ash", "cax51@fsn1"},
        )

    def test_sold_out_locations_are_excluded(self):
        keys = _keys(_cfg())
        self.assertNotIn("cx52@fsn1", keys)  # available: false
        self.assertNotIn("cx52@hel1", keys)  # available: false
        self.assertNotIn("cax41@hel1", keys)  # available: false

    def test_dedicated_cpu_excluded_by_default(self):
        self.assertNotIn("ccx33@fsn1", _keys(_cfg()))

    def test_dedicated_included_when_cpu_types_widened(self):
        self.assertIn("ccx33@fsn1", _keys(_cfg(cpu_types=())))

    def test_undersized_ram_excluded(self):
        self.assertNotIn("cx42@fsn1", _keys(_cfg()))  # 16 GB < 30 GB floor

    def test_max_memory_bounds_the_search(self):
        keys = _keys(_cfg(max_memory_gb=32))
        self.assertIn("cx52@nbg1", keys)
        self.assertNotIn("cax51@fsn1", keys)  # 64 GB is above the ceiling

    def test_price_ceiling_drops_expensive_plans(self):
        # cpx51@ash is 64.90 gross; cax41@fsn1 is 34.97; cx52@nbg1 is 65.33.
        self.assertEqual(_keys(_cfg(max_price_monthly=50.0)), {"cax41@fsn1"})

    def test_price_ceiling_keeps_unpriced_locations(self):
        # cax51 has a price entry, so raise the ceiling above it to confirm the
        # boundary is inclusive rather than accidentally strict.
        self.assertIn("cax51@fsn1", _keys(_cfg(max_price_monthly=71.40)))

    def test_no_ceiling_means_no_price_filtering(self):
        self.assertIn("cpx51@ash", _keys(_cfg(max_price_monthly=None)))

    def test_permanently_retired_type_excluded_even_though_available_is_true(self):
        # cx51@fsn1 has available: true but unavailable_after is in the past.
        self.assertNotIn("cx51@fsn1", _keys(_cfg(include_deprecated=True)))

    def test_deprecated_but_still_buyable_is_opt_in(self):
        self.assertNotIn("cx53@nbg1", _keys(_cfg()))
        self.assertIn("cx53@nbg1", _keys(_cfg(include_deprecated=True)))

    def test_location_filter(self):
        self.assertEqual(_keys(_cfg(locations=("fsn1", "nbg1", "hel1"))),
                         {"cx52@nbg1", "cax41@fsn1", "cax51@fsn1"})

    def test_architecture_filter(self):
        self.assertEqual(_keys(_cfg(architectures=("arm",))), {"cax41@fsn1", "cax51@fsn1"})

    def test_family_filter(self):
        self.assertEqual(_keys(_cfg(families=("cx", "cax"))),
                         {"cx52@nbg1", "cax41@fsn1", "cax51@fsn1"})

    def test_explicit_allowlist_overrides_memory_filters(self):
        # cx42 is 16 GB, below the default floor, but named explicitly.
        self.assertEqual(_keys(_cfg(server_types=("cx42",))), {"cx42@fsn1"})

    def test_category_match_is_a_regex_over_the_category_field(self):
        self.assertEqual(_keys(_cfg(category_match="dedicated", cpu_types=())),
                         {"ccx33@fsn1"})

    def test_offer_carries_price_and_renders_a_line(self):
        offer = next(o for o in iter_matching_offers(_server_types(), _cfg(), now=NOW)
                     if o.key == "cax41@fsn1")
        self.assertEqual(offer.price_monthly_gross, "34.9741000000")
        self.assertIn("34.97 EUR/mo", offer.one_line())
        self.assertIn("32 GB RAM", offer.one_line())
        self.assertIn("arm", offer.one_line())


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "seen.json")
            self.assertEqual(load_seen(path), set())  # missing file is not an error
            save_seen(path, {"cx52@nbg1"})
            self.assertEqual(load_seen(path), {"cx52@nbg1"})

    def test_corrupt_state_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seen.json")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{not json")
            self.assertEqual(load_seen(path), set())


class TestCheckDeduplication(unittest.TestCase):
    def _run(self, tmp, sent, **cfg_kwargs):
        cfg = _cfg(state_file=os.path.join(tmp, "seen.json"),
                   ntfy_topic="unit-test-topic", **cfg_kwargs)
        import check_hetzner

        original = check_hetzner.notify
        check_hetzner.notify = lambda _cfg, **kw: (sent.append(kw), ["ntfy"])[1]
        try:
            return check(cfg,
                         server_types_source=lambda _c: _server_types(),
                         locations_source=lambda _c: {},
                         now=NOW)
        finally:
            check_hetzner.notify = original

    def test_alerts_once_then_stays_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            first = self._run(tmp, sent)
            self.assertEqual(len(first.fresh), 4)
            self.assertEqual(len(sent), 1, "first sighting should alert")

            second = self._run(tmp, sent)
            self.assertEqual(second.fresh, [], "same stock must not re-alert")
            self.assertEqual(len(sent), 1, "no second notification for unchanged stock")

    def test_always_mode_alerts_every_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            self._run(tmp, sent, notify_mode="always")
            self._run(tmp, sent, notify_mode="always")
            self.assertEqual(len(sent), 2)

    def test_never_mode_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            result = self._run(tmp, sent, notify_mode="never")
            self.assertTrue(result.offers)
            self.assertEqual(sent, [])

    def test_notification_body_lists_the_offers(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent = []
            self._run(tmp, sent)
            body = sent[0]["message"]
            self.assertIn("cx52", body)
            self.assertIn("cax41", body)
            self.assertNotIn("ccx33", body)


class TestConfig(unittest.TestCase):
    def test_missing_token_is_a_clear_error(self):
        with self.assertRaises(HetznerWatchError) as ctx:
            Config.from_env({})
        self.assertIn("HCLOUD_TOKEN", str(ctx.exception))

    def test_env_parsing(self):
        cfg = Config.from_env({
            "HCLOUD_TOKEN": "abc",
            "MIN_MEMORY_GB": "60",
            "LOCATIONS": "fsn1, nbg1",
            "CPU_TYPES": "*",
            "NTFY_TOPIC": "my-topic",
        })
        self.assertEqual(cfg.min_memory_gb, 60.0)
        self.assertEqual(cfg.locations, ("fsn1", "nbg1"))
        self.assertEqual(cfg.cpu_types, ())  # "*" means no restriction
        self.assertTrue(cfg.has_notifier)

    def test_bad_notify_mode_rejected(self):
        with self.assertRaises(HetznerWatchError):
            _cfg(notify_mode="shout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
