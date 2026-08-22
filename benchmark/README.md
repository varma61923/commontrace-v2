# benchmark/

The two benchmark scripts that used to live here now ship **inside the
package**, at [`commontrace/reference/`](../commontrace/reference/):

| Script | Run it with |
|---|---|
| `measure_performance.py` — memory health (lesson quality, implicit retrieval, transfer gap) | `commontrace bench` |
| `pilot_metrics.py` — the five business-outcome metrics (repeated-error, resolution, escalation, frustration, cost) | `commontrace bench --pilot` |

They were moved so that a plain `pip install commontrace` can compute its own
numbers. Previously both commands only worked from a repo checkout, which meant
a customer could install the product and still not measure their own pilot —
the one number they most need.

They stay *scripts run as subprocesses* rather than an importable subpackage
(`commontrace/reference/` deliberately has no `__init__.py`); they ship via
`package-data` in `pyproject.toml`.

`STATUS.md` in this directory records the benchmark's methodology and known
limitations, and is still the right place to read before quoting any number.
