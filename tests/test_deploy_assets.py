import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "deploy", "helm", "unimem-hub")
ARGS = ["--set", "image.repository=r.example/hub", "--set", "image.tag=1.0.0", "--set", "existingSecret=s"]


def _rules():
    with open(os.path.join(CHART, "files", "slo-alerts.yaml"), encoding="utf-8") as fh:
        groups = yaml.safe_load(fh)
    return {r["alert"]: r for g in groups for r in g["rules"] if "alert" in r}


def test_the_burn_rate_alerts_use_the_objectives_they_name():
    rules = _rules()
    assert "14.4 * 0.001" in rules["HubAvailabilityFastBurn"]["expr"]
    assert "6 * 0.001" in rules["HubAvailabilitySlowBurn"]["expr"]
    assert rules["HubAvailabilityFastBurn"]["labels"]["slo"] == "availability-99.9"
    pytest.importorskip("hub.observability")
    from hub.observability import Metrics

    assert 250 in Metrics.BUCKETS_MS
    assert "6 * 0.05" in rules["HubLatencyBurn"]["expr"]
    for alert in rules.values():
        if "Burn" in alert["alert"]:
            assert alert["expr"].count(" and ") == 1


def test_every_metric_the_alerts_use_is_one_the_hub_exports():
    import re

    source = Path(os.path.join(ROOT, "hub", "observability.py")).read_text(encoding="utf-8")
    with open(os.path.join(CHART, "files", "slo-alerts.yaml"), encoding="utf-8") as fh:
        used = set(re.findall(r"unimem_hub_[a-z_]+", fh.read()))
    exported = set(re.findall(r"unimem_hub_[a-z_]+", source))
    missing = {
        m
        for m in used
        if not m.startswith("unimem_hub:")
        and re.sub(r"_(bucket|count|sum)$", "", m) not in exported
        and m not in exported
    }
    assert not missing, missing


def test_terraform_state_and_variable_files_are_ignored():
    gi = Path(os.path.join(ROOT, "deploy", "terraform", ".gitignore")).read_text(encoding="utf-8")
    assert "*.tfstate" in gi and "*.tfvars" in gi and ".terraform/" in gi


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
class TestChart:
    def _template(self, *extra):
        return subprocess.run(["helm", "template", "t", CHART, *ARGS, *extra], capture_output=True, text=True)

    def test_it_renders_with_every_optional_template_on(self):
        r = self._template(
            "--set",
            "metrics.alerts=true",
            "--set",
            "metrics.serviceMonitor=true",
            "--set",
            "networkPolicy.enabled=true",
            "--set",
            "ingress.enabled=true",
            "--set",
            "ingress.host=h.example",
            "--set",
            "autoscaling.enabled=true",
        )
        assert r.returncode == 0, r.stderr
        kinds = {d["kind"] for d in yaml.safe_load_all(r.stdout) if d}
        assert {"Deployment", "Job", "Ingress", "NetworkPolicy", "PrometheusRule", "ServiceMonitor"} <= kinds

    def test_restricted_database_egress_can_allow_explicit_https_destinations(self):
        result = self._template(
            "--set",
            "networkPolicy.enabled=true",
            "--set",
            "networkPolicy.postgresCIDR=10.0.0.0/24",
            "--set",
            "networkPolicy.httpsCIDRs[0]=192.0.2.0/24",
        )
        assert result.returncode == 0, result.stderr
        policy = next(doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "NetworkPolicy")
        https = [rule for rule in policy["spec"]["egress"] if {"port": 443, "protocol": "TCP"} in rule.get("ports", [])]
        assert https == [{"to": [{"ipBlock": {"cidr": "192.0.2.0/24"}}], "ports": [{"port": 443, "protocol": "TCP"}]}]

    @pytest.mark.parametrize(
        "flag,message",
        [
            (["--set", "image.tag=latest"], "must not be latest"),
            (["--set", "existingSecret="], "existingSecret is required"),
            (["--set", "config.HUB_RATE_LIMIT_BACKEND=memory"], "HUB_RATE_LIMIT_BACKEND=postgres"),
            (["--set", "postgres.maxConnections=30"], "exceeds postgres.maxConnections"),
            (["--set", "terminationGracePeriodSeconds=30"], "must exceed"),
            (["--set", "config.HUB_READYZ_TIMEOUT_SECONDS=3"], "below the 3s"),
            (["--set", "image.digest=sha256:invalid"], "64 lowercase hex"),
            (["--set", "config.HUB_DATABASE_URL=postgresql://fixture"], "existingSecret, not a ConfigMap"),
            (["--set", "config.HUB_API_KEY_PEPPER=fixture"], "existingSecret, not a ConfigMap"),
        ],
    )
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
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert container["startupProbe"]["httpGet"]["path"] == "/healthz"
        assert spec["terminationGracePeriodSeconds"] > 30

    def test_immutable_image_digest_can_replace_the_tag(self):
        digest = "sha256:" + "a" * 64
        result = self._template("--set", "image.tag=", "--set", f"image.digest={digest}")
        assert result.returncode == 0, result.stderr
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == f"r.example/hub@{digest}"

    def test_custom_metrics_secret_protects_the_server_and_authenticates_the_monitor(self):
        result = self._template("--set", "metrics.serviceMonitor=true", "--set", "metrics.tokenSecretKey=metrics-token")
        assert result.returncode == 0, result.stderr
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
        monitor = next(doc for doc in docs if doc["kind"] == "ServiceMonitor")
        env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        server_key = next(item["valueFrom"]["secretKeyRef"] for item in env if item["name"] == "HUB_METRICS_TOKEN")
        assert server_key == {"name": "s", "key": "metrics-token"}
        assert monitor["spec"]["endpoints"][0]["authorization"]["credentials"] == server_key


def test_standalone_autoscaling_uses_shared_rate_limits():
    config = yaml.safe_load(Path(ROOT, "deploy/k8s/configmap.yaml").read_text())
    hpa = yaml.safe_load(Path(ROOT, "deploy/k8s/hpa.yaml").read_text())
    assert hpa["spec"]["maxReplicas"] > 1
    assert config["data"]["HUB_RATE_LIMIT_BACKEND"] == "postgres"


def test_standalone_migrations_are_restricted_and_one_replica_can_be_drained():
    deployment = yaml.safe_load(Path(ROOT, "deploy/k8s/deployment.yaml").read_text())
    hpa = yaml.safe_load(Path(ROOT, "deploy/k8s/hpa.yaml").read_text())
    pdb = yaml.safe_load(Path(ROOT, "deploy/k8s/pdb.yaml").read_text())
    assert min(deployment["spec"]["replicas"], hpa["spec"]["minReplicas"]) > pdb["spec"]["minAvailable"]
    pod = yaml.safe_load(Path(ROOT, "deploy/k8s/migration-job.yaml").read_text())["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"]
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    for container in pod["containers"]:
        security = container["securityContext"]
        assert security["allowPrivilegeEscalation"] is False and security["readOnlyRootFilesystem"] is True
        assert security["capabilities"]["drop"] == ["ALL"]


def test_compose_forwards_operator_security_configuration_to_the_hub():
    compose = yaml.safe_load(Path(ROOT, "docker-compose.yml").read_text())
    environment = compose["services"]["hub"]["environment"]
    for name in (
        "HUB_API_KEY_PEPPER",
        "HUB_ENCRYPTION_KEY",
        "HUB_LEDGER_SIGNING_KEY",
        "HUB_ADMIN_TOKEN",
        "HUB_METRICS_TOKEN",
    ):
        assert environment[name] == "${" + name + ":-}"
    assert environment["HUB_REQUIRE_API_KEY_PEPPER"] == "${HUB_REQUIRE_API_KEY_PEPPER:-false}"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool not installed")
@pytest.mark.parametrize("business_traffic", [False, True, None])
def test_slo_rules_evaluate_user_traffic_without_probe_dilution_or_idle_latency_alerts(tmp_path, business_traffic):
    groups = yaml.safe_load(Path(CHART, "files/slo-alerts.yaml").read_text())
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump({"groups": groups}))
    series = [
        {"series": 'unimem_hub_requests_total{path="/healthz",status="200"}', "values": "0+100x120"},
        {"series": 'unimem_hub_request_duration_ms_count{path="/healthz"}', "values": "0+100x120"},
        {"series": 'unimem_hub_request_duration_ms_bucket{path="/healthz",le="250"}', "values": "0+100x120"},
    ]
    if business_traffic is None:
        for sample in series:
            sample["values"] = "0+0x120"
    if business_traffic is not False:
        increments = "0+1x120" if business_traffic else "0+0x120"
        series.extend(
            [
                {"series": 'unimem_hub_requests_total{path="/mcp",status="500"}', "values": increments},
                {"series": 'unimem_hub_request_duration_ms_count{path="/mcp"}', "values": increments},
                {"series": 'unimem_hub_request_duration_ms_bucket{path="/mcp",le="250"}', "values": "0+0x120"},
            ]
        )
    expected = int(business_traffic is True)
    checks = [
        {
            "expr": name,
            "eval_time": "65m",
            "exp_samples": [{"labels": name, "value": expected}],
        }
        for name in ("unimem_hub:error_ratio:rate5m", "unimem_hub:slow_ratio:rate5m", "unimem_hub:slow_ratio:rate1h")
    ]
    traffic_alert = _rules()["HubNoTraffic"]
    tests = {
        "rule_files": [str(rules)],
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": "1m",
                "input_series": series,
                "promql_expr_test": checks,
                "alert_rule_test": [
                    {
                        "eval_time": "65m",
                        "alertname": "HubNoTraffic",
                        "exp_alerts": []
                        if business_traffic
                        else [{"exp_labels": traffic_alert["labels"], "exp_annotations": traffic_alert["annotations"]}],
                    }
                ],
            }
        ],
    }
    fixture = tmp_path / "tests.yaml"
    fixture.write_text(yaml.safe_dump(tests))
    result = subprocess.run(["promtool", "test", "rules", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")
@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_terraform_is_formatted(cloud):
    r = subprocess.run(
        ["terraform", f"-chdir={os.path.join(ROOT, 'deploy', 'terraform', cloud)}", "fmt", "-check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout
