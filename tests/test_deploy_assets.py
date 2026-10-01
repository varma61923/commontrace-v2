"""The deployment assets: the SLO alert maths always, and helm/terraform when they are installed
(CI installs both, pinned; a developer machine may not)."""
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "deploy", "helm", "commontrace-hub")
ARGS = ["--set", "image.repository=r.example/hub", "--set", "image.tag=1.0.0", "--set", "existingSecret=s"]


def _rules():
    with open(os.path.join(CHART, "files", "slo-alerts.yaml"), encoding="utf-8") as fh:
        groups = yaml.safe_load(fh)
    return {r["alert"]: r for g in groups for r in g["rules"] if "alert" in r}


def test_the_burn_rate_alerts_use_the_objectives_they_name():
    rules = _rules()
    # 99.9% availability -> a 0.001 budget; fast burn 14.4x (2% of 30 days in 1h), slow burn 6x
    assert "14.4 * 0.001" in rules["HubAvailabilityFastBurn"]["expr"]
    assert "6 * 0.001" in rules["HubAvailabilitySlowBurn"]["expr"]
    assert rules["HubAvailabilityFastBurn"]["labels"]["slo"] == "availability-99.9"
    # 95% under 250 ms -> a 0.05 budget; the 250 ms bucket must be one the Hub actually exports
    pytest.importorskip("starlette")  # hub/ needs its own requirements; the core-install job has none
    from hub.observability import Metrics
    assert 250 in Metrics.BUCKETS_MS
    assert "6 * 0.05" in rules["HubLatencyBurn"]["expr"]
    for alert in rules.values():  # each burn alert needs a long AND a short window
        if "Burn" in alert["alert"]:
            assert alert["expr"].count(" and ") == 1


def test_every_metric_the_alerts_use_is_one_the_hub_exports():
    import re
    source = open(os.path.join(ROOT, "hub", "observability.py"), encoding="utf-8").read()
    with open(os.path.join(CHART, "files", "slo-alerts.yaml"), encoding="utf-8") as fh:
        used = set(re.findall(r"commontrace_hub_[a-z_]+", fh.read()))
    exported = set(re.findall(r"commontrace_hub_[a-z_]+", source))
    missing = {m for m in used if not m.startswith("commontrace_hub:") and
               re.sub(r"_(bucket|count|sum)$", "", m) not in exported and m not in exported}
    assert not missing, missing


def test_terraform_state_and_variable_files_are_ignored():
    gi = open(os.path.join(ROOT, "deploy", "terraform", ".gitignore"), encoding="utf-8").read()
    assert "*.tfstate" in gi and "*.tfvars" in gi and ".terraform/" in gi


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestChart:
    def _template(self, *extra):
        return subprocess.run(["helm", "template", "t", CHART, *ARGS, *extra], capture_output=True, text=True)

    def test_it_renders_with_every_optional_template_on(self):
        r = self._template("--set", "metrics.alerts=true", "--set", "metrics.serviceMonitor=true",
                           "--set", "networkPolicy.enabled=true", "--set", "ingress.enabled=true",
                           "--set", "ingress.host=h.example", "--set", "autoscaling.enabled=true")
        assert r.returncode == 0, r.stderr
        kinds = {d["kind"] for d in yaml.safe_load_all(r.stdout) if d}
        assert {"Deployment", "Job", "Ingress", "NetworkPolicy", "PrometheusRule", "ServiceMonitor"} <= kinds

    @pytest.mark.parametrize("flag,message", [
        (["--set", "image.tag=latest"], "must not be latest"),
        (["--set", "existingSecret="], "existingSecret is required"),
        (["--set", "config.HUB_RATE_LIMIT_BACKEND=memory"], "HUB_RATE_LIMIT_BACKEND=postgres"),
        (["--set", "postgres.maxConnections=30"], "exceeds postgres.maxConnections"),
    ])
    def test_it_refuses_unsafe_combinations(self, flag, message):
        r = self._template(*flag)
        assert r.returncode != 0 and message in r.stderr

    def test_no_secret_value_is_ever_rendered_and_the_pod_is_locked_down(self):
        r = self._template()
        docs = [d for d in yaml.safe_load_all(r.stdout) if d]
        assert not [d for d in docs if d["kind"] == "Secret"]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        spec = dep["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        container = spec["containers"][0]
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"      # never the database
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_terraform_is_formatted(cloud):
    r = subprocess.run(["terraform", f"-chdir={os.path.join(ROOT, 'deploy', 'terraform', cloud)}", "fmt",
                        "-check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
