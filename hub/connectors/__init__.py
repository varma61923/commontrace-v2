"""Outcome connectors: systems of record -> `record_occasion_outcome`."""
from __future__ import annotations

from hub.connectors import github, greenhouse, intercom, zendesk

PROVIDERS = {m.name: m for m in (zendesk, github, greenhouse, intercom)}
