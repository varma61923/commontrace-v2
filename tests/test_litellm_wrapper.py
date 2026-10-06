
from commontrace.litellm_wrapper import CommonTraceLiteLLM, wrap_completion


def test_augment_messages_prepends_system():
    wrapper = CommonTraceLiteLLM()
    messages = [{"role": "user", "content": "How do I deploy?"}]
    augmented = wrapper._augment_messages(messages, "User prefers Docker over Kubernetes.")

    assert len(augmented) == 2
    assert augmented[0]["role"] == "system"
    assert "User prefers Docker over Kubernetes." in augmented[0]["content"]
    assert augmented[1]["content"] == "How do I deploy?"


def test_augment_messages_extends_existing_system():
    wrapper = CommonTraceLiteLLM()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    augmented = wrapper._augment_messages(messages, "User timezone is UTC+2.")

    assert len(augmented) == 2
    assert augmented[0]["role"] == "system"
    assert "You are a helpful assistant." in augmented[0]["content"]
    assert "User timezone is UTC+2." in augmented[0]["content"]


def test_completion_with_custom_fn(tmp_path):
    root = str(tmp_path / "store")
    wrapper = CommonTraceLiteLLM(root=root, auto_extract=False)

    called_with = {}

    def mock_completion(model, messages, **kwargs):
        called_with["model"] = model
        called_with["messages"] = messages
        return {"choices": [{"message": {"role": "assistant", "content": "I am ready."}}]}

    resp = wrapper.completion(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Test prompt"}],
        completion_fn=mock_completion,
    )
    assert resp["choices"][0]["message"]["content"] == "I am ready."
    assert called_with["model"] == "gpt-4o"
    assert len(called_with["messages"]) >= 1


def test_wrap_completion_factory():
    fn = wrap_completion()

    def mock_completion(model, messages, **kwargs):
        return {"result": "ok", "model": model}

    # Pass mock completion_fn via kwargs
    res = fn("claude-sonnet-5", [{"role": "user", "content": "hi"}], completion_fn=mock_completion)
    assert res["result"] == "ok"
    assert res["model"] == "claude-sonnet-5"
