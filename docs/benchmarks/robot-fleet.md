# Robot fleet demo

An end-to-end embodied fleet through the real gateway, fleet tooling and causal
engine: four simulated and two real warehouse-picking robots, each with its own
edge store adopted from its environment's coordinator.

On every occasion a robot posts a multimodal episode (`/v1/episode`: summary,
sensor readings, a wrist-camera PNG), recalls its candidate memories
(`/v1/recall`) and reports the outcome (`/v1/outcome`). `safety:` memories are
protected: always delivered, never randomized, so they never get a verdict.

The seeded truth differs by environment on purpose: the aisle-7 shortcut is
harmless in simulation and costs 20 points on the real floor.

## Result (seed 0, 1,200 occasions per environment)

| Fleet | Memory | Verdict | Effect [anytime-valid 95% CI] | True average effect |
| --- | --- | --- | --- | --: |
| sim | grip:soft-items-slow | NO_MEASURABLE_EFFECT | +0.111 [-0.036, +0.255] | +0.125 |
| sim | path:aisle-7-shortcut | NO_MEASURABLE_EFFECT | -0.007 [-0.153, +0.139] | 0.000 |
| sim | light:recalibrate-camera | NO_MEASURABLE_EFFECT | -0.016 [-0.161, +0.131] | 0.000 |
| real | grip:soft-items-slow | HELPS | +0.155 [+0.006, +0.299] | +0.125 |
| real | path:aisle-7-shortcut | **HURTS** | -0.186 [-0.328, -0.037] | -0.200 |
| real | light:recalibrate-camera | NO_MEASURABLE_EFFECT | +0.002 [-0.146, +0.149] | 0.000 |

- 2,400 multimodal episodes recorded as governed traces (media content-addressed).
- Merging a simulated robot's logs with a real robot's is **refused**: pooling
  would average the shortcut's harm away.
- A central real-fleet gateway with `on_harm=withdraw` then delivered the
  shortcut 0 times in 100 occasions, the safety memory 100 times.
- On-robot recall latency through the gateway: p50 0.55 ms, p95 0.75 ms
  (budget 25 ms) on a shared 4-core CPU.

This is a seeded simulation on one machine, not a measurement of physical
robots. The simulated fleet's grip memory is underpowered at this volume,
which is the honest reading: no verdict yet, not "no effect".

## Reproduce

```bash
python -m benchmarks.robot_fleet_demo --occasions 1200 --seed 0 --out fleet.json
```
