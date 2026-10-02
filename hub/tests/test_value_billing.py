import pytest

from hub import billing, plans, value_billing

pytestmark = pytest.mark.asyncio

LINE = {"kind": "value", "amount": 123.456, "evidence": {
    "experiment": "salt-1", "ledger_root": "r" * 64, "data_digest": "d" * 64, "verify": "commontrace proof verify x"}}
TEST = billing.StripeSettings(secret_key="sk_test_123")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_post(path, key, data):
        calls.append((path, key, data))
        return {"object": "billing.meter_event"}

    monkeypatch.setattr(billing, "_post", fake_post)
    return calls


async def test_a_value_line_is_sent_as_an_integer_in_minor_units_with_a_stable_identifier(sent):
    await value_billing.report_value_line(TEST, org_id="o1", plan="team", customer_id="cus_1", line=LINE,
                                          event_name="proven_value")
    await value_billing.report_value_line(TEST, org_id="o1", plan="team", customer_id="cus_1", line=LINE,
                                          event_name="proven_value")
    (path, _key, data), (_, _, again) = sent
    assert path == "billing/meter_events" and data["payload[value]"] == "12346"
    assert data["payload[stripe_customer_id]"] == "cus_1" and data["event_name"] == "proven_value"
    assert data["identifier"] == again["identifier"]
    other = value_billing.identifier_for({**LINE, "evidence": {**LINE["evidence"], "ledger_root": "x" * 64}}, "o1")
    assert other != data["identifier"] and value_billing.identifier_for(LINE, "o2") != data["identifier"]


@pytest.mark.parametrize("plan,allowed", [("free", False), ("team", True), ("scale", True), ("operator", False),
                                          ("unknown", False)])
async def test_each_plan_either_reports_value_or_is_refused(sent, plan, allowed):
    call = value_billing.report_value_line(TEST, org_id="o", plan=plan, customer_id="cus_1", line=LINE,
                                           event_name="e")
    if allowed:
        await call
        assert len(sent) == 1
    else:
        with pytest.raises(value_billing.ValueBillingRefused, match="not billable"):
            await call
        assert sent == []


@pytest.mark.parametrize("plan_path", [("free", "team"), ("team", "scale"), ("scale", "team"), ("team", "free")])
async def test_a_plan_change_changes_what_is_reported_from_that_point_only(sent, plan_path):
    before, after = plan_path
    for plan in (before, after):
        try:
            await value_billing.report_value_line(TEST, org_id="o", plan=plan, customer_id="cus_1", line=LINE,
                                                  event_name="e")
        except value_billing.ValueBillingRefused:
            pass
    reported = len(sent)
    assert reported == sum(p in plans.BILLABLE_PLANS for p in plan_path)


@pytest.mark.parametrize("line,message", [
    ({**LINE, "kind": "platform"}, "only a value line"), ({**LINE, "kind": "credit", "amount": -5}, "only a value"),
    ({**LINE, "amount": 0}, "not positive"), ({**LINE, "amount": -3}, "not positive"),
    ({**LINE, "evidence": {}}, "without its evidence"),
    ({**LINE, "evidence": {**LINE["evidence"], "verify": ""}}, "without its evidence")])
async def test_only_a_positive_evidenced_value_line_is_reported(sent, line, message):
    with pytest.raises(value_billing.ValueBillingRefused, match=message):
        await value_billing.report_value_line(TEST, org_id="o", plan="team", customer_id="cus_1", line=line,
                                              event_name="e")
    assert sent == []


async def test_a_live_key_is_refused_unless_it_is_allowed_on_purpose(sent):
    live = billing.StripeSettings(secret_key="sk_live_abc")
    with pytest.raises(value_billing.ValueBillingRefused, match="live Stripe key"):
        await value_billing.report_value_line(live, org_id="o", plan="team", customer_id="cus_1", line=LINE,
                                              event_name="e")
    await value_billing.report_value_line(live, org_id="o", plan="team", customer_id="cus_1", line=LINE,
                                          event_name="e", allow_live=True)
    assert len(sent) == 1


@pytest.mark.parametrize("customer,event,message", [("acct_1", "e", "cus_"), ("cus_1", " ", "event_name"),
                                                    ("", "e", "cus_")])
async def test_the_customer_and_the_meter_must_be_named(sent, customer, event, message):
    with pytest.raises(value_billing.ValueBillingRefused, match=message):
        await value_billing.report_value_line(TEST, org_id="o", plan="team", customer_id=customer, line=LINE,
                                              event_name=event)


async def test_no_key_means_nothing_is_sent(sent):
    with pytest.raises(value_billing.ValueBillingRefused, match="no Stripe secret key"):
        await value_billing.report_value_line(billing.StripeSettings(), org_id="o", plan="team",
                                              customer_id="cus_1", line=LINE, event_name="e")
