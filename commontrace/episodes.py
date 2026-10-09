"""Multimodal episodes from robots and devices: one occasion's summary, sensors and media.

An episode is what a fleet member saw and did on one occasion. It is stored as a
schema-valid trace (so it is retrievable, forgettable and governed like any
other) tagged with its environment, so simulation and reality stay apart.
Media is stored content-addressed under ``memory/media/`` and described in the
trace by kind, digest, size and, for images, the dimensions read from the file
header; nothing is decoded or executed. Sensor readings become a sorted,
bounded ``name: value`` table, so two episodes with the same readings produce
the same text.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
import os
import re

from commontrace import paths, trace_io

MAX_SENSORS = 64
MAX_MEDIA = 8
MAX_MEDIA_BYTES = 2 * 1024 * 1024
MEDIA_KINDS = {"image": ("image/png", "image/jpeg"), "audio": ("audio/wav", "audio/mpeg", "audio/ogg"),
               "video": ("video/mp4", "video/webm")}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "audio/wav": "wav", "audio/mpeg": "mp3", "audio/ogg": "ogg",
        "video/mp4": "mp4", "video/webm": "webm"}


class EpisodeError(ValueError):
    pass


def _sensor_value(name: str, value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise EpisodeError(f"sensor {name!r} is not a finite number")
        return f"{value:g}"
    if isinstance(value, str) and len(value) <= 128:
        return value.replace("\n", " ")
    raise EpisodeError(f"sensor {name!r} must be a number, boolean or short text")


def _image_dims(data: bytes, mime: str) -> dict:
    from commontrace.ingest import multimodal

    try:
        width, height, _info = (multimodal._parse_png(data) if mime == "image/png"
                                else multimodal._parse_jpeg(data))
    except (ValueError, IndexError, Exception):  # noqa: BLE001 - a header we cannot read is refused below
        raise EpisodeError(f"the {mime} data does not parse as that format") from None
    return {"width": width, "height": height}


def _store_media(root: str, item: dict, n: int) -> dict:
    if not isinstance(item, dict):
        raise EpisodeError(f"media[{n}] must be an object")
    kind, mime = item.get("kind"), item.get("mime")
    if kind not in MEDIA_KINDS or mime not in MEDIA_KINDS[kind]:
        raise EpisodeError(f"media[{n}] needs kind in {sorted(MEDIA_KINDS)} and a matching mime type")
    caption = str(item.get("caption") or "").strip()
    if len(caption) > 1000:
        raise EpisodeError(f"media[{n}].caption is longer than 1000 characters")
    try:
        data = base64.b64decode(str(item.get("data_b64") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise EpisodeError(f"media[{n}].data_b64 is not base64") from None
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise EpisodeError(f"media[{n}] must be 1 byte to {MAX_MEDIA_BYTES // (1024 * 1024)} MiB")
    described = {"kind": kind, "mime": mime, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                 "caption": caption}
    if kind == "image":
        described.update(_image_dims(data, mime))
    directory = os.path.join(paths.memory_dir(root), "media")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{described['sha256']}.{_EXT[mime]}")
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    return described


def record(root: str, *, occasion_id: str, summary: str, agent_id: str = "", env: str | None = None,
           sensors: dict | None = None, media: list | None = None, outcome: bool | None = None) -> dict:
    """Store one episode as a trace; returns its id and the media it described."""
    summary = (summary or "").strip()
    if not summary:
        raise EpisodeError("an episode needs a summary of what happened")
    sensors = sensors or {}
    if not isinstance(sensors, dict) or len(sensors) > MAX_SENSORS:
        raise EpisodeError(f"sensors must be an object of at most {MAX_SENSORS} readings")
    for name in sensors:
        if not isinstance(name, str) or not _NAME.match(name):
            raise EpisodeError(f"sensor name {name!r} must be 1-64 letters, digits, _ . : / -")
    readings = [f"- {name}: {_sensor_value(name, sensors[name])}" for name in sorted(sensors)]
    media = media or []
    if not isinstance(media, list) or len(media) > MAX_MEDIA:
        raise EpisodeError(f"media must be a list of at most {MAX_MEDIA} items")
    described = [_store_media(root, item, n) for n, item in enumerate(media)]
    lines = [summary]
    if readings:
        lines += ["", "Sensors:", *readings]
    for m in described:
        dims = f", {m['width']}x{m['height']}" if m.get("width") else ""
        lines.append(f"{m['kind'].capitalize()} {m['sha256'][:12]} ({m['mime']}, {m['bytes']} bytes{dims})"
                     + (f": {m['caption']}" if m["caption"] else ""))
    tags = ["episode"] + ([f"env:{env}"] if env else []) + sorted({f"media:{m['kind']}" for m in described})
    path = trace_io.write_new(
        root, title=f"Episode {occasion_id}"[:200], context="\n".join(lines), solution=summary, tags=tags,
        outcome=None if outcome is None else {"resolved": bool(outcome)},
        extra={**({"agent_id": agent_id} if agent_id else {}),
               "extensions": {"episode": {"occasion_id": occasion_id, "env": env,
                                          "sensors": {k: sensors[k] for k in sorted(sensors)}, "media": described}}})
    if path is None:
        raise EpisodeError("the episode could not be written")
    trace, _ = trace_io.read(path)
    return {"episode_id": trace["id"], "occasion_id": occasion_id, "media": described,
            "sensors": len(readings), "env": env}
