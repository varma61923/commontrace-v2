#!/usr/bin/env python3
"""Render an explicitly public, signed aggregate report; never publish raw trajectories."""

import argparse
import html
import json
import os

from benchmarks.signed_results import verify


def render(envelope: dict, public_key: str) -> str:
    report = verify(envelope, trusted_key=public_key)
    rows = []
    for gate in report["gates"]:
        values = [gate["name"], gate["status"], gate.get("measurement", ""), gate.get("scope", "")]
        rows.append("<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in values) + "</tr>")
    return (
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>UniMem measured results</title>
<style>body{font:16px system-ui;max-width:1100px;margin:4rem auto;padding:1rem;
background:#10151e;color:#e9edf3}table{border-collapse:collapse;width:100%}td,th{padding:1rem;text-align:left;
border-bottom:1px solid #354056}a{color:#99caff}caption{text-align:left;margin:1rem 0}</style>
<main><h1>UniMem measured results</h1><p>Evidence before claims. Fixture scores are labelled; external quality and
coding uplift remain unverified until their required evaluations run.</p>
<table><caption>Current measured release gates</caption>
<thead><tr><th>Gate</th><th>Status</th><th>Measurement</th><th>Scope</th></tr></thead>
<tbody>"""
        + "".join(rows)
        + """</tbody></table>
<p><a href="run.json">Signed aggregate report</a> · <a href="public-key.txt">Pinned publisher key</a></p>
<p>The repository-pinned session signing key identifies this report; its signature does not certify scientific validity.
Private transcripts and signing keys are excluded.</p></main></html>"""
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--public-key", "--trusted-key", dest="public_key", required=True,
                        help="Ed25519 public key (verification key) to verify and export")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    with open(args.report, encoding="utf-8") as stream:
        envelope = json.load(stream)
    page = render(envelope, args.public_key)
    os.makedirs(args.out, exist_ok=True)
    for name, content in [
        ("index.html", page),
        ("run.json", json.dumps(envelope, indent=2) + "\n"),
        ("public-key.txt", args.public_key + "\n"),
    ]:
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as stream:
            stream.write(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
