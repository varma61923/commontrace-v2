"""Bounded MCAP and LeRobot metadata ingest without decoding media in the Hub."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from commontrace import episodes
from commontrace import frontier_protocol as fp


def ingest_records(root: str, records: Iterable[dict[str, Any]], *, format: str,
                   environment: str, robot: str) -> list[dict[str, Any]]:
    if format not in ("mcap", "lerobot") or environment not in ("sim", "real"):
        raise ValueError("declare MCAP/LeRobot format and sim/real environment")
    fp.text(robot, "robot", 64)
    result = []
    total = 0
    for index, row in enumerate(records):
        fp.count(index, 0, 999)
        size = len(fp.wire(row))
        total += size
        if size > 8192 or total > 8 * fp.MAX_BYTES:
            raise ValueError("robot metadata ingest exceeds bounded record or batch size")
        occasion = fp.text(str(row.get("occasion_id", row.get("episode_index", ""))))
        summary = fp.text(row.get("summary", "Robot episode " + occasion), "episode summary", 4096)
        success = row.get("success", row.get("next.success"))
        if success is not None and type(success) is not bool:
            raise ValueError("success label must be binary or absent")
        sensors = dict(row.get("sensors", {}))
        refs = row.get("media_refs", [])
        fp.count(len(refs), 0, 8)
        for reference in refs:
            fp.text(reference, "content-addressed media reference", 256)
            if not reference.startswith("sha256:") or len(reference) != 71:
                raise ValueError("media references require sha256 content addresses")
        if refs:
            summary += "\nMedia references: " + ", ".join(refs)
        result.append(episodes.record(root, occasion_id=occasion, summary=summary, agent_id=robot,
                                       env=environment, sensors=sensors, outcome=success))
    return result


def lerobot_jsonl(root: str, path: str, *, environment: str, robot: str) -> list[dict[str, Any]]:
    if Path(path).stat().st_size > 8 * fp.MAX_BYTES:
        raise ValueError("LeRobot metadata file exceeds 8 MiB")
    def records() -> Iterable[dict[str, Any]]:
        with open(path, "rb") as stream:
            while line := stream.readline(8193):
                if len(line) > 8192:
                    raise ValueError("LeRobot record exceeds 8 KiB")
                yield json.loads(line)
    return ingest_records(root, records(), format="lerobot", environment=environment, robot=robot)


def mcap_file(root: str, path: str, *, environment: str, robot: str,
              topics: tuple[str, ...] = ("/commontrace/episode",)) -> list[dict[str, Any]]:
    try:
        from mcap.records import Channel, Chunk, Message
        from mcap.stream_reader import StreamReader, breakup_chunk
    except ImportError:
        raise RuntimeError("install commontrace[mcap] for MCAP container ingest") from None
    if Path(path).stat().st_size > 64 * fp.MAX_BYTES:
        raise ValueError("MCAP file exceeds 64 MiB; segment it at the edge")
    # Bound decompression before parsing a container; the MCAP reader's default
    # LZ4 path has no decompressed-output cap. Never invoke a media decoder.
    with open(path, "rb") as stream:
        reader = StreamReader(stream, emit_chunks=True, validate_crcs=True, record_size_limit=8 * fp.MAX_BYTES)
        channels: dict[int, Any] = {}
        total_decoded = 0

        def records() -> Iterable[dict[str, Any]]:
            nonlocal total_decoded
            for record in reader.records:
                batch = [record]
                if isinstance(record, Chunk):
                    if record.uncompressed_size > 8 * fp.MAX_BYTES:
                        raise ValueError("MCAP decompressed chunk exceeds 8 MiB")
                    if record.compression == "zstd":
                        try:
                            import zstandard
                        except ImportError:
                            raise RuntimeError("install commontrace[mcap] with zstandard support") from None
                        with zstandard.ZstdDecompressor().stream_reader(record.data) as decoder:
                            data = decoder.read(8 * fp.MAX_BYTES + 1)
                    elif record.compression == "lz4":
                        try:
                            from lz4.frame import LZ4FrameDecompressor
                        except ImportError:
                            raise RuntimeError("install commontrace[mcap] with lz4 support") from None
                        data = LZ4FrameDecompressor().decompress(record.data, max_length=8 * fp.MAX_BYTES + 1)
                    elif record.compression == "":
                        data = record.data
                    else:
                        raise ValueError("unsupported MCAP container compression")
                    total_decoded += len(data)
                    if len(data) != record.uncompressed_size or total_decoded > 8 * fp.MAX_BYTES:
                        raise ValueError("MCAP decompressed batch exceeds its declared bounded size")
                    from dataclasses import replace
                    batch = breakup_chunk(replace(record, compression="", data=data), validate_crc=True)
                for item in batch:
                    if isinstance(item, Channel):
                        fp.count(len(channels), 0, 127, "MCAP channels")
                        channels[item.id] = item
                    elif isinstance(item, Message):
                        channel = channels.get(item.channel_id)
                        if channel is None or channel.topic not in topics:
                            continue
                        if channel.message_encoding != "json" or len(item.data) > 8192:
                            raise ValueError("MCAP episode channels require bounded JSON metadata")
                        yield json.loads(item.data)
        return ingest_records(root, records(), format="mcap", environment=environment, robot=robot)
