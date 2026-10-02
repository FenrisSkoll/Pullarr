"""Opt-in normal-entrypoint fixture, never imported by ordinary application."""

from unittest.mock import patch

from fixtures.reading_orders import FixtureConnection, FixtureLists
from fixtures.release_calendar import (CalendarFixture,
                                       CalendarGCD, CalendarMetron)

from backend.implementations import reading_order_sources
from backend.implementations.metadata.registry import PROVIDERS

PROVIDERS.update(comicvine=CalendarFixture, metron=CalendarMetron, gcd=CalendarGCD)
original = reading_order_sources.CBLFetcher.__init__


def fixture_fetcher(self):
    original(self, resolver=lambda host: ['93.184.216.34'], connection=FixtureConnection)


fetcher_patch = patch.object(reading_order_sources.CBLFetcher, '__init__', fixture_fetcher)
provider_patch = patch.object(reading_order_sources, 'MetronLists', FixtureLists)
fetcher_patch.start()
provider_patch.start()
