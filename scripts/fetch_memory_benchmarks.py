"""Download the official long-term memory benchmarks at pinned revisions and verify them.

    python scripts/fetch_memory_benchmarks.py --dest data/benchmarks
    python scripts/fetch_memory_benchmarks.py --dest data/benchmarks --only locomo,beam_100K

Nothing is bundled with UniMem: LoCoMo is CC BY-NC 4.0 and the others carry their own
licences, so each file is fetched from its publisher at an exact revision and refused
unless its SHA-256 matches. Hugging Face downloads use HF_TOKEN when it is set.
BEAM-10M is published as two shards; they are kept as shards and merged on request
(--merge-beam-10m, which needs pyarrow).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request

_HF = "https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"
_GITHUB = "https://raw.githubusercontent.com/{repo}/{revision}/{path}"

# name: (url, sha256)
DATASETS: dict[str, tuple[str, str]] = {
    "locomo10.json": (
        _GITHUB.format(repo="snap-research/locomo", revision="cbfbc1dba6bc53d00625212a0f22d55ffee7c1fc",
                       path="data/locomo10.json"),
        "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"),
    "longmemeval_s_cleaned.json": (
        _HF.format(repo="xiaowu0162/longmemeval-cleaned", revision="98d7416c24c778c2fee6e6f3006e7a073259d48f",
                   path="longmemeval_s_cleaned.json"),
        "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"),
    "beam_100K.parquet": (
        _HF.format(repo="Mohammadta/BEAM", revision="3205395e897e7318c7b094ef4e6047b9b82dbb03",
                   path="data/100K-00000-of-00001.parquet"),
        "c0519be25907005ba873c927c50877471d550873039d96c041554d0075a78ace"),
    "beam_500K.parquet": (
        _HF.format(repo="Mohammadta/BEAM", revision="3205395e897e7318c7b094ef4e6047b9b82dbb03",
                   path="data/500K-00000-of-00001.parquet"),
        "af05921c979355038e1761b7cde3d2dd713200dd3071b278de0200f6c7f30122"),
    "beam_1M.parquet": (
        _HF.format(repo="Mohammadta/BEAM", revision="3205395e897e7318c7b094ef4e6047b9b82dbb03",
                   path="data/1M-00000-of-00001.parquet"),
        "41b5acbbb55a586b1305514ef9d9fb03365d9b3331b598a1c2dd7603d93ef533"),
    "beam_10M_0.parquet": (
        _HF.format(repo="Mohammadta/BEAM-10M", revision="9b2096193fe74e2837e4713e483351e19817773c",
                   path="data/10M-00000-of-00002.parquet"),
        "31d96fd47ec56221d202e68792f26c00e49467dd4b36ee105c36ebd19ef78ad5"),
    "beam_10M_1.parquet": (
        _HF.format(repo="Mohammadta/BEAM-10M", revision="9b2096193fe74e2837e4713e483351e19817773c",
                   path="data/10M-00001-of-00002.parquet"),
        "a4f13fe25af51d57405ae41008689c31d1421377f3efde56a024b441deb2ee65"),
}


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(name: str, dest: str, *, opener=urllib.request.urlopen) -> str:
    url, expected = DATASETS[name]
    target = os.path.join(dest, name)
    if os.path.isfile(target) and _sha256(target) == expected:
        return target
    headers = {"User-Agent": "unimem-benchmarks"}
    token = os.environ.get("HF_TOKEN", "").strip()
    if token and url.startswith("https://huggingface.co/"):
        headers["Authorization"] = f"Bearer {token}"
    partial = target + ".partial"
    with opener(urllib.request.Request(url, headers=headers), timeout=600) as response, \
            open(partial, "wb") as sink:  # nosec B310 - fixed https URLs above
        for block in iter(lambda: response.read(1 << 20), b""):
            sink.write(block)
    actual = _sha256(partial)
    if actual != expected:
        os.remove(partial)
        raise RuntimeError(f"{name}: SHA-256 {actual} does not match the pinned {expected}")
    os.replace(partial, target)
    return target


def merge_beam_10m(dest: str) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    shards = [pq.read_table(os.path.join(dest, f"beam_10M_{i}.parquet")) for i in (0, 1)]
    target = os.path.join(dest, "beam_10M.parquet")
    pq.write_table(pa.concat_tables(shards, promote_options="default"), target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dest", required=True)
    parser.add_argument("--only", default="", help="comma list of dataset names (prefix match), default all")
    parser.add_argument("--merge-beam-10m", action="store_true")
    args = parser.parse_args(argv)
    wanted = [n for n in DATASETS if not args.only or any(n.startswith(p) for p in args.only.split(",") if p)]
    if not wanted:
        parser.error("--only matched no dataset")
    os.makedirs(args.dest, exist_ok=True)
    for name in wanted:
        print(f"{name}: {fetch(name, args.dest)}", file=sys.stderr)
    if args.merge_beam_10m:
        print(f"beam_10M.parquet: {merge_beam_10m(args.dest)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
