"""Outcome connectors: systems of record -> `record_occasion_outcome`.

See hub/connectors/base.py for the model. Providers are plain modules exposing
`name`, `validate_config`, `verify`, `delivery_id` and `signals`; adding one is
one file and one line below.
"""
from __future__ import annotations

from hub.connectors import github, greenhouse, intercom, zendesk

PROVIDERS = {m.name: m for m in (zendesk, github, greenhouse, intercom)}
