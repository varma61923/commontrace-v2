from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from hub.connectors import PROVIDERS, base, github, zendesk

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "connectors")
NOW = datetime(2025, 1, 10, 17, 30, tzinfo=timezone.utc)
ZD_SECRET = "dGhpc19zZWNyZXRfaXNfZm9yX3Rlc3Rpbmdfb25seQ=="
GH_SECRET = "It's a Secret to Everybody"


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _zd_headers(body: bytes, secret=ZD_SECRET, stamp="2025-01-10T17:29:00Z"):
    sig = base64.b64encode(hmac.new(secret.encode(), stamp.encode() + body, hashlib.sha256).digest()).decode()
    return {"X-Zendesk-Webhook-Signature": sig, "X-Zendesk-Webhook-Signature-Timestamp": stamp}


def _gh_headers(body: bytes, event: str, secret=GH_SECRET, delivery="72d3162e-cc78-11e3-81ab-4c9367dc0958"):
    return {
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
    }


def _zd_status(current, previous):
    payload = _fixture("zendesk_ticket_status_changed.json")
    payload["event"]["current"], payload["event"]["previous"] = current, previous
    return payload


class TestZendeskSignature:
    BODY = json.dumps(_fixture("zendesk_ticket_status_changed.json")).encode()

    def test_a_correctly_signed_delivery_verifies(self):
        zendesk.verify(_zd_headers(self.BODY), self.BODY, ZD_SECRET, now=NOW)

    def test_header_names_are_case_insensitive(self):
        lowered = {k.lower(): v for k, v in _zd_headers(self.BODY).items()}
        zendesk.verify(lowered, self.BODY, ZD_SECRET, now=NOW)

    def test_the_secret_is_not_base64_decoded(self):
        decoded = base64.b64decode(ZD_SECRET).decode()
        with pytest.raises(base.SignatureError):
            zendesk.verify(_zd_headers(self.BODY, secret=decoded), self.BODY, ZD_SECRET, now=NOW)

    @pytest.mark.parametrize("mutate", [
        lambda b: b + b" ", lambda b: b.replace(b"41960", b"41961") if b"41960" in b else b + b"x",
    ])
    def test_a_tampered_body_is_refused(self, mutate):
        with pytest.raises(base.SignatureError):
            zendesk.verify(_zd_headers(self.BODY), mutate(self.BODY), ZD_SECRET, now=NOW)

    def test_a_wrong_secret_is_refused(self):
        with pytest.raises(base.SignatureError):
            zendesk.verify(_zd_headers(self.BODY, secret="another"), self.BODY, ZD_SECRET, now=NOW)

    def test_a_signed_timestamp_must_be_the_one_in_the_header(self):
        headers = _zd_headers(self.BODY)
        headers["X-Zendesk-Webhook-Signature-Timestamp"] = "2025-01-10T17:29:30Z"
        with pytest.raises(base.SignatureError):
            zendesk.verify(headers, self.BODY, ZD_SECRET, now=NOW)

    @pytest.mark.parametrize("stamp, ok", [
        ("2025-01-10T17:29:00Z", True),
        ("2025-01-10T17:25:01Z", True),
        ("2025-01-10T17:24:59Z", False),
        ("2025-01-10T17:36:00Z", False),
    ])
    def test_a_validly_signed_but_stale_delivery_is_a_replay(self, stamp, ok):
        headers = _zd_headers(self.BODY, stamp=stamp)
        if ok:
            zendesk.verify(headers, self.BODY, ZD_SECRET, now=NOW)
        else:
            with pytest.raises(base.SignatureError, match="replay window"):
                zendesk.verify(headers, self.BODY, ZD_SECRET, now=NOW)

    def test_missing_or_garbled_headers_are_refused(self):
        with pytest.raises(base.SignatureError):
            zendesk.verify({}, self.BODY, ZD_SECRET, now=NOW)
        with pytest.raises(base.SignatureError):
            zendesk.verify(_zd_headers(self.BODY, stamp="yesterday"), self.BODY, ZD_SECRET, now=NOW)

    def test_the_replay_key_is_the_events_own_id(self):
        payload = _fixture("zendesk_ticket_status_changed.json")
        assert zendesk.delivery_id({}, payload) == payload["id"]
        with pytest.raises(base.SignatureError) as exc:
            zendesk.delivery_id({}, {})
        assert exc.value.status == 400


@pytest.mark.parametrize("text, expected", [
    ("2025-01-10T17:27:48.105316520+00:00", "2025-01-10T17:27:48.105316+00:00"),
    ("2025-01-10T17:27:48.999999999-05:00", "2025-01-10T17:27:48.999999-05:00"),
    ("2025-01-10T17:27:48.1Z", "2025-01-10T17:27:48.100000+00:00"),
    ("2025-01-10T17:27:48Z", "2025-01-10T17:27:48+00:00"),
    ("2025-01-10T17:27:48", "2025-01-10T17:27:48+00:00"),
])
def test_vendor_timestamps_parse_on_every_supported_python(text, expected):
    assert base.parse_timestamp(text).isoformat() == expected


class TestZendeskSignals:
    CFG = zendesk.validate_config({})

    def test_the_vendors_own_new_to_open_example_is_not_an_outcome(self):
        assert zendesk.signals({}, _fixture("zendesk_ticket_status_changed.json"), self.CFG) == []

    def test_solved_is_a_candidate_keyed_by_ticket_id(self):
        [sig] = zendesk.signals({}, _zd_status("SOLVED", "OPEN"), self.CFG)
        assert (sig.kind, sig.occasion_id) == (base.CANDIDATE, "1244")
        assert sig.event_id == "ca74d97a-ae8c-40e9-b8a8-37e3ae0afedf"

    def test_closed_after_solved_is_not_a_second_candidate(self):
        assert zendesk.signals({}, _zd_status("CLOSED", "SOLVED"), self.CFG) == []

    def test_reopening_a_solved_ticket_is_a_reversal(self):
        [sig] = zendesk.signals({}, _zd_status("OPEN", "SOLVED"), self.CFG)
        assert (sig.kind, sig.occasion_id) == (base.REVERSAL, "1244")

    def test_pending_to_open_is_nothing(self):
        assert zendesk.signals({}, _zd_status("OPEN", "PENDING"), self.CFG) == []

    def test_a_good_csat_is_not_a_success_and_a_bad_one_is_a_failure(self):
        good = _fixture("zendesk_ticket_csat_received.json")
        assert good["event"]["satisfaction_score"]["score"] == "GOOD"
        assert zendesk.signals({}, good, self.CFG) == []
        bad = copy.deepcopy(good)
        bad["event"]["satisfaction_score"]["score"] = "BAD"
        [sig] = zendesk.signals({}, bad, self.CFG)
        assert (sig.kind, sig.occasion_id) == (base.FAILURE, "74184")

    def test_other_event_types_are_ignored(self):
        payload = _zd_status("SOLVED", "OPEN")
        payload["type"] = "zen:event-type:ticket.comment_added"
        assert zendesk.signals({}, payload, self.CFG) == []

    def test_the_occasion_prefix_is_applied(self):
        cfg = zendesk.validate_config({"occasion_prefix": "ZD-"})
        assert zendesk.signals({}, _zd_status("SOLVED", "OPEN"), cfg)[0].occasion_id == "ZD-1244"

    @pytest.mark.parametrize("bad_id", [None, "", {"x": 1}, True, "x" * 300])
    def test_a_payload_without_a_usable_ticket_id_raises(self, bad_id):
        payload = _zd_status("SOLVED", "OPEN")
        payload["detail"]["id"] = bad_id
        with pytest.raises(ValueError):
            zendesk.signals({}, payload, self.CFG)


def _gh_merged():
    payload = _fixture("github_pull_request_closed.json")
    payload["pull_request"]["merged"] = True
    payload["pull_request"]["merged_at"] = payload["pull_request"]["closed_at"]
    return payload


class TestGithubSignature:
    BODY = json.dumps(_fixture("github_pull_request_closed.json")).encode()

    def test_a_correctly_signed_delivery_verifies(self):
        github.verify(_gh_headers(self.BODY, "pull_request"), self.BODY, GH_SECRET, now=NOW)

    def test_it_matches_githubs_published_test_vector(self):
        body = b"Hello, World!"
        headers = {"X-Hub-Signature-256":
                   "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"}
        github.verify(headers, body, GH_SECRET, now=NOW)

    @pytest.mark.parametrize("supplied", ["", "sha1=abc", "sha256=", "sha256=" + "0" * 64, "nope"])
    def test_a_bad_or_missing_signature_is_refused(self, supplied):
        with pytest.raises(base.SignatureError):
            github.verify({"X-Hub-Signature-256": supplied}, self.BODY, GH_SECRET, now=NOW)

    def test_a_tampered_body_or_wrong_secret_is_refused(self):
        headers = _gh_headers(self.BODY, "pull_request")
        with pytest.raises(base.SignatureError):
            github.verify(headers, self.BODY + b"\n", GH_SECRET, now=NOW)
        with pytest.raises(base.SignatureError):
            github.verify(headers, self.BODY, "other", now=NOW)

    def test_a_delivery_without_a_delivery_id_has_no_replay_protection_and_is_refused(self):
        with pytest.raises(base.SignatureError) as exc:
            github.delivery_id({"X-GitHub-Event": "push"}, {})
        assert exc.value.status == 400


class TestGithubSignals:
    CFG = github.validate_config({})

    def test_a_pr_closed_without_merging_is_a_failure(self):
        payload = _fixture("github_pull_request_closed.json")
        assert payload["pull_request"]["merged"] is False
        [sig] = github.signals(_gh_headers(b"", "pull_request"), payload, self.CFG)
        assert (sig.kind, sig.occasion_id) == (base.FAILURE, "2")

    def test_a_merged_pr_is_a_candidate_that_remembers_its_merge_commit(self):
        [sig] = github.signals(_gh_headers(b"", "pull_request"), _gh_merged(), self.CFG)
        assert (sig.kind, sig.occasion_id) == (base.CANDIDATE, "2")
        assert sig.ref == "c4295bd74fb0f4fda03689c3df3f2803b658fd85"

    def test_a_push_with_gits_revert_message_is_a_reversal_by_sha(self):
        payload = _fixture("github_push_master.json")
        payload["commits"][0]["message"] = (
            'Revert "Update the README"\n\nThis reverts commit '
            "c4295bd74fb0f4fda03689c3df3f2803b658fd85.\n")
        [sig] = github.signals(_gh_headers(b"", "push"), payload, self.CFG)
        assert (sig.kind, sig.occasion_id, sig.ref) == (
            base.REVERSAL, "", "c4295bd74fb0f4fda03689c3df3f2803b658fd85")

    def test_the_vendors_own_push_example_is_not_a_reversal(self):
        assert github.signals(_gh_headers(b"", "push"), _fixture("github_push_master.json"), self.CFG) == []

    def test_a_short_sha_in_a_message_is_not_a_reversal(self):
        payload = _fixture("github_push_master.json")
        payload["commits"][0]["message"] = "This reverts commit c4295bd."
        assert github.signals(_gh_headers(b"", "push"), payload, self.CFG) == []

    def test_other_events_and_actions_are_ignored(self):
        payload = _fixture("github_pull_request_closed.json")
        payload["action"] = "labeled"
        assert github.signals(_gh_headers(b"", "pull_request"), payload, self.CFG) == []
        assert github.signals(_gh_headers(b"", "issues"), payload, self.CFG) == []

    def test_a_repository_filter_ignores_other_repos(self):
        cfg = github.validate_config({"repository": "someone/else"})
        assert github.signals(_gh_headers(b"", "pull_request"), _gh_merged(), cfg) == []
        cfg = github.validate_config({"repository": "Codertocat/Hello-World"})
        assert len(github.signals(_gh_headers(b"", "pull_request"), _gh_merged(), cfg)) == 1


@pytest.mark.parametrize("provider, config", [
    ("zendesk", {"surprise": 1}), ("zendesk", {"window_days": -1}), ("zendesk", {"window_days": True}),
    ("zendesk", {"occasion_prefix": 5}), ("zendesk", "text"), ("github", {"repository": "no-slash"}),
    ("github", {"repository": "a/b/c"}), ("github", {"window_days": 9999}),
])
def test_bad_connector_config_is_refused(provider, config):
    with pytest.raises(base.ConfigError):
        PROVIDERS[provider].validate_config(config)


def test_every_provider_exposes_the_whole_interface():
    for module in PROVIDERS.values():
        for attr in ("name", "validate_config", "verify", "delivery_id", "signals"):
            assert hasattr(module, attr), (module.__name__, attr)


def test_the_reopen_window_arithmetic_is_not_off_by_a_tolerance():
    assert base.TOLERANCE_SECONDS == 300
    assert timedelta(seconds=base.TOLERANCE_SECONDS) == timedelta(minutes=5)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(detail="x"), lambda p: p.update(event=["x"]), lambda p: p.update(detail=None),
])
def test_zendesk_payload_of_the_wrong_shape_raises_a_value_error_not_an_attribute_error(mutate):
    payload = _zd_status("SOLVED", "OPEN")
    mutate(payload)
    with pytest.raises(ValueError):
        zendesk.signals({}, payload, zendesk.validate_config({}))


def test_github_payload_of_the_wrong_shape_is_handled():
    cfg = github.validate_config({})
    payload = _gh_merged()
    payload["pull_request"] = "x"
    with pytest.raises(ValueError):
        github.signals(_gh_headers(b"", "pull_request"), payload, cfg)
    push = _fixture("github_push_master.json")
    push["commits"] = "not a list"
    assert github.signals(_gh_headers(b"", "push"), push, cfg) == []
    push["commits"] = [None, 3, {"message": None}]
    assert github.signals(_gh_headers(b"", "push"), push, cfg) == []


from hub.connectors import greenhouse  # noqa: E402

GHS_SECRET = "greenhouse-secret-key"


def _ghs_headers(body: bytes, secret=GHS_SECRET, event_id="evt-uuid-1"):
    headers = {"Signature": "sha256 " + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}
    if event_id:
        headers["Greenhouse-Event-ID"] = event_id
    return headers


class TestGreenhouse:
    BODY = json.dumps(_fixture("greenhouse_hire_candidate.json")).encode()

    def test_a_correctly_signed_delivery_verifies(self):
        greenhouse.verify(_ghs_headers(self.BODY), self.BODY, GHS_SECRET, now=NOW)

    @pytest.mark.parametrize("supplied", [
        "", "sha256=abc", "sha256", "sha1 " + "0" * 40, "sha256 " + "0" * 64,
    ])
    def test_a_bad_signature_is_refused(self, supplied):
        with pytest.raises(base.SignatureError):
            greenhouse.verify({"Signature": supplied}, self.BODY, GHS_SECRET, now=NOW)

    def test_the_whole_body_is_signed_so_any_change_is_refused(self):
        with pytest.raises(base.SignatureError):
            greenhouse.verify(_ghs_headers(self.BODY), self.BODY + b" ", GHS_SECRET, now=NOW)
        with pytest.raises(base.SignatureError):
            greenhouse.verify(_ghs_headers(self.BODY, secret="x"), self.BODY, GHS_SECRET, now=NOW)

    def test_the_documented_event_id_is_the_replay_key(self):
        assert greenhouse.delivery_id({"Greenhouse-Event-ID": "abc"}, {}) == "abc"

    def test_a_delivery_with_no_event_id_is_keyed_by_its_content_not_accepted_unprotected(self):
        a = greenhouse.delivery_id({}, {"x": 1, "y": 2})
        assert a == greenhouse.delivery_id({}, {"y": 2, "x": 1}) and a.startswith("body:")
        assert a != greenhouse.delivery_id({}, {"x": 1, "y": 3})

    def test_a_hire_is_a_candidate_keyed_by_application_id(self):
        [sig] = greenhouse.signals(_ghs_headers(b""), _fixture("greenhouse_hire_candidate.json"),
                                   greenhouse.validate_config({}))
        assert (sig.kind, sig.occasion_id) == (base.CANDIDATE, "46194062")

    def test_a_rejection_is_a_failure(self):
        [sig] = greenhouse.signals(_ghs_headers(b""), _fixture("greenhouse_reject_candidate.json"),
                                   greenhouse.validate_config({}))
        assert (sig.kind, sig.occasion_id) == (base.FAILURE, "265293")

    def test_a_stage_change_counts_only_into_a_stage_the_customer_named(self):
        payload = _fixture("greenhouse_candidate_stage_change.json")
        stage = payload["payload"]["application"]["current_stage"]["name"]
        assert greenhouse.signals(_ghs_headers(b""), payload, greenhouse.validate_config({})) == []
        cfg = greenhouse.validate_config({"success_stages": [stage.upper()]})
        [sig] = greenhouse.signals(_ghs_headers(b""), payload, cfg)
        assert (sig.kind, sig.occasion_id) == (base.SUCCESS, "265277")
        other = greenhouse.validate_config({"success_stages": ["Onsite"]})
        assert greenhouse.signals(_ghs_headers(b""), payload, other) == []

    def test_an_unhire_reverses(self):
        payload = _fixture("greenhouse_hire_candidate.json")
        payload["action"] = "unhire_candidate"
        [sig] = greenhouse.signals(_ghs_headers(b""), payload, greenhouse.validate_config({}))
        assert sig.kind == base.REVERSAL

    def test_the_ping_and_other_actions_are_ignored(self):
        cfg = greenhouse.validate_config({})
        assert greenhouse.signals({}, {"configuration": {"url": "x"}}, cfg) == []
        assert greenhouse.signals({}, {"action": "application_updated", "payload": {}}, cfg) == []

    def test_a_payload_of_the_wrong_shape_raises_a_value_error(self):
        with pytest.raises(ValueError):
            greenhouse.signals({}, {"action": "hire_candidate", "payload": "x"}, greenhouse.validate_config({}))

    @pytest.mark.parametrize("bad", [{"success_stages": "Onsite"}, {"success_stages": [1]},
                                     {"success_stages": [" "]}, {"success_stages": ["x"] * 51}])
    def test_bad_stage_config_is_refused(self, bad):
        with pytest.raises(base.ConfigError):
            greenhouse.validate_config(bad)


from hub.connectors import intercom  # noqa: E402

IC_SECRET = "intercom-client-secret"
IC_NOW = datetime.fromtimestamp(1392731400, tz=timezone.utc)


def _ic_headers(body: bytes, secret=IC_SECRET):
    return {"X-Hub-Signature": "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()}


def _ic(topic, conversation="1295", created_at=1392731331):
    payload = _fixture("intercom_notification_company_created.json")
    payload["topic"], payload["created_at"] = topic, created_at
    payload["data"]["item"] = {"type": "conversation", "id": conversation, "state": "closed"}
    return payload


class TestIntercom:
    def test_a_correctly_signed_notification_verifies(self):
        body = json.dumps(_ic("conversation.admin.closed")).encode()
        intercom.verify(_ic_headers(body), body, IC_SECRET, now=IC_NOW)

    @pytest.mark.parametrize("supplied", ["", "sha256=abc", "sha1=", "sha1=" + "0" * 40, "nope"])
    def test_a_bad_signature_is_refused(self, supplied):
        body = json.dumps(_ic("conversation.admin.closed")).encode()
        with pytest.raises(base.SignatureError):
            intercom.verify({"X-Hub-Signature": supplied}, body, IC_SECRET, now=IC_NOW)

    def test_a_tampered_body_or_wrong_secret_is_refused(self):
        body = json.dumps(_ic("conversation.admin.closed")).encode()
        with pytest.raises(base.SignatureError):
            intercom.verify(_ic_headers(body), body + b" ", IC_SECRET, now=IC_NOW)
        with pytest.raises(base.SignatureError):
            intercom.verify(_ic_headers(body, secret="x"), body, IC_SECRET, now=IC_NOW)

    @pytest.mark.parametrize("age, ok", [
        (60, True), (2 * 3600, True),
        (3 * 3600 + 5, False),
        (-60, True), (-3600, False),
    ])
    def test_freshness_is_bounded_by_the_signed_created_at(self, age, ok):
        created = 1392731331
        body = json.dumps(_ic("conversation.admin.closed", created_at=created)).encode()
        now = datetime.fromtimestamp(created + age, tz=timezone.utc)
        if ok:
            intercom.verify(_ic_headers(body), body, IC_SECRET, now=now)
        else:
            with pytest.raises(base.SignatureError, match="freshness"):
                intercom.verify(_ic_headers(body), body, IC_SECRET, now=now)

    def test_the_notification_id_is_the_replay_key(self):
        payload = _ic("conversation.admin.closed")
        assert intercom.delivery_id({}, payload) == "notif_ccd8a4d0-f965-11e3-a367-c779cae3e1b3"
        with pytest.raises(base.SignatureError) as exc:
            intercom.delivery_id({}, {})
        assert exc.value.status == 400

    def test_closed_is_a_candidate_and_a_reopen_or_customer_reply_reverses(self):
        cfg = intercom.validate_config({})
        [closed] = intercom.signals({}, _ic("conversation.admin.closed"), cfg)
        assert (closed.kind, closed.occasion_id) == (base.CANDIDATE, "1295")
        for topic in ("conversation.admin.opened", "conversation.user.replied"):
            [rev] = intercom.signals({}, _ic(topic), cfg)
            assert (rev.kind, rev.occasion_id) == (base.REVERSAL, "1295")

    def test_the_vendors_own_company_event_and_ping_are_ignored(self):
        cfg = intercom.validate_config({})
        assert intercom.signals({}, _fixture("intercom_notification_company_created.json"), cfg) == []
        assert intercom.signals({}, {"topic": "ping"}, cfg) == []
        assert intercom.signals({}, _ic("conversation.admin.replied"), cfg) == []

    def test_a_payload_of_the_wrong_shape_raises_a_value_error(self):
        payload = _ic("conversation.admin.closed")
        payload["data"] = "x"
        with pytest.raises(ValueError):
            intercom.signals({}, payload, intercom.validate_config({}))
