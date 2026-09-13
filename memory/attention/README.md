# memory/attention/

The two attention scripts that used to live here now ship **inside the
package**, at [`commontrace/reference/`](../../commontrace/reference/):

| Script | Run it with |
|---|---|
| `query.py` — semantic retrieval over the embedding index | `commontrace query "<task>"` |
| `build_index.py` — (re)build `index.npz` from active lessons | `commontrace index` |

They were moved for the same reason the benchmark scripts were (see
[`benchmark/README.md`](../../benchmark/README.md)): a plain
`pip install commontrace[attention]` could install the extra and still not
reach semantic retrieval, because the scripts shipped only in a repo
checkout. `commontrace query` would choose the semantic path, fail to find
`query.py`, and exit non-zero — so the retriever the docs recommend was
unreachable by the install the docs recommend. It also broke for a repo
checkout whose *store* lived elsewhere (`--dest /some/other/path`), since the
lookup is relative to the store root, not the repo.

They stay *scripts run as subprocesses* rather than an importable subpackage
(`commontrace/reference/` deliberately has no `__init__.py`); they ship via
`package-data` in `pyproject.toml`.

This directory still holds each store's own **`index.npz`** — the embedding
index itself, which is per-store data, not code.

Note that the scripts' heavy dependencies (numpy + sentence-transformers) are
still optional. Shipping the *files* costs nothing; whether they can *run* is
a separate question, answered by `has_attention_deps()` before either is ever
invoked, and by `commontrace doctor`.
