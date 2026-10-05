"""Local, socket-free profile of webhook recipient response buffering.

Run from any directory: python research/security-phase6-webhook-profile.py
The baseline reproduces the previous send implementation's response handling.
"""
from __future__ import annotations

import asyncio
import json
import socket
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub import events  # noqa: E402

RESPONSE_MIB = 64
SAMPLES = 3


class RecipientBody(httpx.AsyncByteStream):
    def __init__(self):
        self.read_chunks = 0
        self.closed = False

    async def __aiter__(self):
        chunk = b"x" * (1024 * 1024)
        for _ in range(RESPONSE_MIB):
            self.read_chunks += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def public_dns(host):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]


async def baseline(url, body, headers):
    await events._reject_private_target(url)
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False, transport=transport) as client:
        response = await client.post(url, content=body, headers=headers)
        response.raise_for_status()


async def main():
    streams = []

    async def response(transport, request):
        stream = RecipientBody()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    results = {}
    with patch.object(events, "_default_resolve", public_dns), patch.object(
        httpx.AsyncHTTPTransport, "handle_async_request", response,
    ):
        for name, send in (("buffered_baseline", baseline), ("streaming_fixed", events.http_transport())):
            elapsed, peaks, reads = [], [], []
            await send("https://recipient.example/hook", "{}", {})
            for _ in range(SAMPLES):
                tracemalloc.start()
                start = time.perf_counter()
                await send("https://recipient.example/hook", "{}", {})
                elapsed.append((time.perf_counter() - start) * 1000)
                peaks.append(tracemalloc.get_traced_memory()[1] / (1024 * 1024))
                tracemalloc.stop()
                reads.append(streams[-1].read_chunks)
                assert streams[-1].closed
            results[name] = {
                "median_ms": round(statistics.median(elapsed), 3),
                "median_peak_mib": round(statistics.median(peaks), 3),
                "response_mib_consumed_per_sample": reads,
            }
    print(json.dumps({"recipient_response_mib": RESPONSE_MIB, "samples": SAMPLES, "results": results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
