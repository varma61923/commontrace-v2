from __future__ import annotations

from sqlalchemy import BigInteger

from hub.models import Trace


class TestUnboundedCountersAreBigInteger:
    def test_retrievals_depth_commons_hits_are_bigint(self):
        for column_name in ("retrievals", "depth", "commons_hits"):
            column = Trace.__table__.columns[column_name]
            assert isinstance(column.type, BigInteger), (
                f"Trace.{column_name} is {column.type!r}, expected BigInteger"
            )
