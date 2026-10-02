"""Opt-in disposable acceptance process only, enabled by its PYTHONPATH."""

from fixtures.release_calendar import (CalendarFixture,
                                       CalendarGCD, CalendarMetron)

from backend.implementations.metadata.registry import PROVIDERS

PROVIDERS.update(comicvine=CalendarFixture, metron=CalendarMetron, gcd=CalendarGCD)
