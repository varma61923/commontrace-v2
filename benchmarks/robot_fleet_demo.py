"""An end-to-end embodied fleet: multimodal episodes, protected safety memory, sim/real separation, causal report.

Six warehouse picking robots run through the real gateway: four in simulation,
two on the floor. Each robot keeps its own edge store, adopted from its
environment's coordinator so every robot shares one randomization. On each
occasion a robot:

1. posts a multimodal episode (`/v1/episode`): a summary, sensor readings and
   a camera frame (a real PNG);
2. recalls its candidate memories (`/v1/recall`, items mode). ``safety:`` memories
   are protected: always delivered, never randomized;
3. reports how the pick went (`/v1/outcome`).

The true effects are seeded and differ between environments on purpose:
``path:aisle-7-shortcut`` is harmless in simulation (no people there) and hurts
on the real floor. That is the sim-to-real gap pooling would hide.

Afterwards the demo merges each environment's edge logs (`fleet merge`), shows
that merging simulation with reality is refused, reads each environment's
anytime-valid verdicts, then runs the real fleet through a central gateway with
``on_harm=withdraw`` and shows the harmful memory leaving deliveries while the
safety memory never does. On-robot recall latency is reported against a budget.

    python -m benchmarks.robot_fleet_demo --out fleet.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import shutil
import struct
import sys
import tempfile
import time
import zlib

TOKEN = "f" * 40
MEMORIES = {
    "safety:estop-near-humans": "Stop all motion when a person is within 1.5 m.",
    "grip:soft-items-slow": "Grip fragile items at half force and lift slowly.",
    "path:aisle-7-shortcut": "Cut through aisle 7 to save travel time.",
    "light:recalibrate-camera": "Recalibrate the camera when lighting changes.",
}
# Seeded truth: success-rate change when a memory is delivered, per environment.
TRUTH = {
    "sim": {"grip:soft-items-slow": 0.25, "path:aisle-7-shortcut": 0.0, "light:recalibrate-camera": 0.0},
    "real": {"grip:soft-items-slow": 0.25, "path:aisle-7-shortcut": -0.20, "light:recalibrate-camera": 0.0},
}
FRAGILE_SHARE = 0.5  # the grip memory only acts on fragile picks
ROBOTS = {"sim": ["sim-01", "sim-02", "sim-03", "sim-04"], "real": ["floor-01", "floor-02"]}
LATENCY_BUDGET_MS = 25.0


def _png(width: int, height: int, shade: int) -> bytes:
    raw = b"".join(b"\x00" + bytes([shade]) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class _Robot:
    def __init__(self, gateway_mod, root: str, robot_id: str, env: str, *, on_harm=None):
        self.gw = gateway_mod.Gateway(root, token=TOKEN, durable=False, on_harm=on_harm, check_every=1)
        self.id, self.env, self.latencies = robot_id, env, []

    def call(self, path: str, body: dict) -> dict:
        response = self.gw.handle("POST", path, {"Authorization": "Bearer " + TOKEN, "Host": "localhost"},
                                  json.dumps(body).encode())
        payload = json.loads(response.body)
        if response.status != 200:
            raise RuntimeError(f"{path} -> {response.status}: {payload}")
        return payload

    def occasion(self, n: int, rng: random.Random) -> dict:
        occasion = f"{self.id}-{n}"
        fragile = rng.random() < FRAGILE_SHARE
        human_m = round(rng.uniform(0.5, 6.0), 2)
        self.call("/v1/episode", {
            "occasion_id": occasion, "agent_id": self.id, "env": self.env,
            "summary": f"Pick of a {'fragile' if fragile else 'bulk'} item; nearest person {human_m} m.",
            "sensors": {"grip_force_n": round(rng.uniform(5, 40), 1), "human_distance_m": human_m,
                        "lighting_lux": rng.choice([200, 450, 900]), "fragile": fragile},
            "media": [{"kind": "image", "mime": "image/png", "caption": "wrist camera at grasp",
                       "data_b64": base64.b64encode(_png(16, 12, rng.randrange(256))).decode()}],
        })
        started = time.perf_counter()
        recalled = self.call("/v1/recall", {
            "occasion_id": occasion, "agent_id": self.id, "env": self.env,
            "items": [{"id": k, "text": v} for k, v in MEMORIES.items()]})
        self.latencies.append((time.perf_counter() - started) * 1000)
        delivered = {item["id"] for item in recalled["deliver"]}
        p = 0.55 + sum(effect for memory, effect in TRUTH[self.env].items()
                       if memory in delivered and (memory != "grip:soft-items-slow" or fragile))
        self.call("/v1/outcome", {"occasion_id": occasion, "agent_id": self.id, "env": self.env,
                                  "succeeded": rng.random() < max(0.0, min(1.0, p))})
        return {"delivered": delivered, "protected": set(recalled["protected"]),
                "withdrawn": {w["id"] for w in recalled["withdrawn"]}}


def _verdicts(root: str) -> dict:
    from commontrace import experiment
    from commontrace.commands import experiment_cmd

    rows, _rate, _corrupt = experiment_cmd._load(root)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, rows)
    effects = experiment.analyze(experiment_cmd._observations(rows), sequential=True)
    return {e.lesson_slug: {"verdict": e.verdict, "effect": round(e.effect, 3), "ci": [round(e.ci_low, 3),
            round(e.ci_high, 3)], "n": [e.n_injected, e.n_withheld]} for e in effects}


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1)))], 2)


def run(*, occasions: int = 300, seed: int = 0, workdir: str | None = None) -> dict:
    from commontrace import fleet, gateway, holdout_io

    base = workdir or tempfile.mkdtemp(prefix="robot-fleet-")
    rng = random.Random(seed)
    try:
        robots: dict[str, list[_Robot]] = {}
        for env, members in ROBOTS.items():
            coordinator = os.path.join(base, f"coordinator-{env}")
            holdout_io.configure(coordinator, rate=0.5, salt=f"fleet-{env}-{seed}")
            gateway.save_config(coordinator, gateway.GatewayConfig(env=env, protected_prefixes=("safety:",)))
            robots[env] = []
            for robot_id in members:
                root = os.path.join(base, robot_id)
                fleet.adopt(coordinator, root)
                robots[env].append(_Robot(gateway, root, robot_id, env))
        per_robot = occasions // len(ROBOTS["sim"])
        for env, members in robots.items():
            count = per_robot if env == "sim" else occasions // len(members)
            for robot in members:
                for n in range(count):
                    seen = robot.occasion(n, rng)
                    assert "safety:estop-near-humans" in seen["delivered"]  # never withheld

        merged, verdicts = {}, {}
        for env, members in ROBOTS.items():
            merged[env] = os.path.join(base, f"fleet-{env}")
            fleet.merge([os.path.join(base, r) for r in members], merged[env])
            verdicts[env] = _verdicts(merged[env])
        try:
            fleet.merge([os.path.join(base, ROBOTS["sim"][0]), os.path.join(base, ROBOTS["real"][0])],
                        os.path.join(base, "pooled"))
            pooling = {"refused": False}
        except fleet.FleetError as exc:
            pooling = {"refused": True, "reason": str(exc)[:300]}

        # Phase 2: the real fleet through one central gateway that withdraws proven harm.
        central = _Robot(gateway, merged["real"], "floor-central", "real", on_harm="withdraw")
        deliveries = {k: 0 for k in MEMORIES}
        withdrawn_seen: set[str] = set()
        for n in range(100):
            seen = central.occasion(10_000 + n, rng)
            withdrawn_seen |= seen["withdrawn"]
            for memory in seen["delivered"]:
                deliveries[memory] += 1
        latencies = [ms for members in robots.values() for robot in members for ms in robot.latencies]
        episodes = sum(1 for env in ROBOTS for r in ROBOTS[env]
                       for name in os.listdir(os.path.join(base, r, "memory", "traces")) if name.endswith(".md"))
    finally:
        if workdir is None:
            shutil.rmtree(base, ignore_errors=True)

    with open(os.path.abspath(__file__), "rb") as fh:
        import hashlib

        source = hashlib.sha256(fh.read()).hexdigest()
    p95 = _percentile(latencies, 0.95)
    return {
        "demo": "robot-fleet", "version": 1, "source_sha256": source, "seed": seed, "truth": TRUTH,
        # What an unconditional comparison estimates: the grip effect diluted by the fragile share.
        "truth_average": {env: {m: e * (FRAGILE_SHARE if m == "grip:soft-items-slow" else 1.0)
                                for m, e in effects.items()} for env, effects in TRUTH.items()},
        "robots": ROBOTS, "occasions_per_env": {env: occasions for env in ROBOTS}, "episodes_recorded": episodes,
        "verdicts": verdicts, "pooling_sim_with_real": pooling,
        "harm_withdrawal": {"withdrawn": sorted(withdrawn_seen), "deliveries_in_100_occasions": deliveries},
        "recall_latency_ms": {"p50": _percentile(latencies, 0.5), "p95": p95, "budget_p95": LATENCY_BUDGET_MS,
                              "within_budget": p95 <= LATENCY_BUDGET_MS, "calls": len(latencies)},
        "note": "Seeded simulation through the real gateway, fleet tooling and causal engine on one machine; "
                "not a measurement of any physical robot.",
    }


def render(report: dict) -> str:
    lines = [f"Robot fleet demo v{report['version']} (seed {report['seed']}): "
             f"{report['episodes_recorded']} multimodal episodes", ""]
    for env, verdicts in report["verdicts"].items():
        lines.append(f"{env} fleet ({', '.join(report['robots'][env])}):")
        for memory, v in sorted(verdicts.items()):
            truth = report["truth_average"][env].get(memory)
            lines.append(f"  {memory:<26} {v['verdict']:<22} {v['effect']:+.3f} [{v['ci'][0]:+.3f}, "
                         f"{v['ci'][1]:+.3f}]  true average {truth:+.3f}")
        lines.append("  safety:estop-near-humans   protected: always delivered, never randomized")
    pooled = report["pooling_sim_with_real"]
    lines += ["", f"Pooling sim with real: {'refused' if pooled['refused'] else 'ALLOWED (bug)'}",
              f"Central real gateway, on_harm=withdraw: withdrew {', '.join(report['harm_withdrawal']['withdrawn']) or 'nothing'}; "
              f"deliveries in 100 occasions {report['harm_withdrawal']['deliveries_in_100_occasions']}"]
    lat = report["recall_latency_ms"]
    lines.append(f"On-robot recall latency p50 {lat['p50']} ms, p95 {lat['p95']} ms "
                 f"(budget {lat['budget_p95']} ms: {'met' if lat['within_budget'] else 'MISSED'})")
    lines += ["", report["note"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--occasions", type=int, default=1200, help="occasions per environment")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    report = run(occasions=args.occasions, seed=args.seed)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(render(report))
    return 0 if report["pooling_sim_with_real"]["refused"] else 1


if __name__ == "__main__":
    sys.exit(main())
