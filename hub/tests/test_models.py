from __future__ import annotations

from sqlalchemy import BigInteger

from hub.models import Trace


class TestUnboundedCountersAreBigInteger:
    """retrievals/depth/commons_hits are monotonic, never-decremented,
    never-reset counters on rows that can live for the life of a
    long-running Hub. A plain 32-bit Integer caps at ~2.1 billion and
    Postgres raises "integer out of range" the moment an UPDATE ... SET
    x = x + 1 would cross it, turning an ordinary call into an unhandled
    500 for every future request touching that row. Pinned here as a type
    check so a future edit can't quietly narrow one of these back to
    Integer -- see hub/alembic/versions/40d3f29cbb24_*.py for the migration
    that widened them."""

    def test_retrievals_depth_commons_hits_are_bigint(self):
        for column_name in ("retrievals", "depth", "commons_hits"):
            column = Trace.__table__.columns[column_name]
            assert isinstance(column.type, BigInteger), (
                f"Trace.{column_name} is {column.type!r}, expected BigInteger"
            )
