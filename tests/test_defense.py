import pytest

from commontrace.defense import (
    DefenseAction,
    DefensePolicy,
    PolicyRule,
    apply_redaction,
    parse_policy,
    screen_content,
)


def test_apply_redaction_ai_keys():
    text = (
        "Here is Anthropic sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234 and "
        "OpenAI sk-abcdefghijklmnopqrstuvwxyz12345678 and "
        "Google AIzaSyD12345678901234567890123456789012 key."
    )
    result = apply_redaction(text)
    assert "anthropic_key" in result.matched_types
    assert "openai_key" in result.matched_types
    assert "google_api_key" in result.matched_types
    assert "[REDACTED:anthropic_key]" in result.content
    assert "[REDACTED:openai_key]" in result.content
    assert "[REDACTED:google_api_key]" in result.content

    # Previews must never expose full secret
    for hit in result.hits:
        assert hit["preview"] != text
        assert "..." in hit["preview"]


def test_apply_redaction_cloud_and_tokens():
    slack_tok = "-".join(["xoxb", "1234567890", "123456789012", "abcdefghijklmnopqrstuvwx"])
    gh_tok = "ghp_" + ("a" * 36)
    text = (
        f"AWS AKIAIOSFODNN7EXAMPLE with session ASIAIOSFODNN7EXAMPLE and "
        f"GitHub {gh_tok} and Slack {slack_tok}."
    )
    result = apply_redaction(text)
    assert "aws_access_key" in result.matched_types
    assert "aws_session_token" in result.matched_types
    assert "github_token" in result.matched_types
    assert "slack_token" in result.matched_types


def test_luhn_credit_card_validation():
    # Valid Visa card passes Luhn
    valid_card = "4111 1111 1111 1111"
    # Invalid card (fails checksum)
    invalid_card = "4111 1111 1111 1112"

    res_valid = apply_redaction(f"Card: {valid_card}")
    assert "credit_card" in res_valid.matched_types
    assert "[REDACTED:credit_card]" in res_valid.content

    res_invalid = apply_redaction(f"Card: {invalid_card}")
    assert "credit_card" not in res_invalid.matched_types
    assert invalid_card in res_invalid.content


def test_uuid_does_not_trigger_credit_card():
    uuid_str = "12345678-1234-1234-1234-123456789012"
    result = apply_redaction(f"Object id: {uuid_str}")
    assert "credit_card" not in result.matched_types
    assert uuid_str in result.content


def test_screen_content_policies():
    secret_text = "My key is sk-ant-abcdefghijklmnopqrstuvwxyz1234"

    # Default REDACT policy
    redact_pol = DefensePolicy(
        enabled=True,
        rules=(PolicyRule(on="sensitive_data", action=DefenseAction.REDACT),),
    )
    dec_redact = screen_content(secret_text, policy=redact_pol)
    assert dec_redact.action == DefenseAction.REDACT
    assert "[REDACTED:anthropic_key]" in (dec_redact.redacted_content or "")

    # BLOCK policy
    block_pol = DefensePolicy(
        enabled=True,
        rules=(PolicyRule(on="sensitive_data", action=DefenseAction.BLOCK),),
    )
    dec_block = screen_content(secret_text, policy=block_pol)
    assert dec_block.action == DefenseAction.BLOCK
    assert dec_block.redacted_content is None

    # ALLOW policy
    allow_pol = DefensePolicy(
        enabled=True,
        rules=(PolicyRule(on="sensitive_data", action=DefenseAction.ALLOW),),
    )
    dec_allow = screen_content(secret_text, policy=allow_pol)
    assert dec_allow.action == DefenseAction.ALLOW

    # Disabled policy
    dis_pol = DefensePolicy(enabled=False)
    dec_dis = screen_content(secret_text, policy=dis_pol)
    assert dec_dis.action == DefenseAction.ALLOW


def test_parse_policy_valid_and_invalid():
    pol = parse_policy({
        "enabled": True,
        "rules": [{"on": "sensitive_data", "action": "block"}],
    })
    assert pol.enabled is True
    assert pol.rules[0].action == DefenseAction.BLOCK

    with pytest.raises(ValueError, match="invalid action"):
        parse_policy({"rules": [{"on": "sensitive_data", "action": "destroy"}]})

    with pytest.raises(ValueError, match="invalid on"):
        parse_policy({"rules": [{"on": "", "action": "redact"}]})
