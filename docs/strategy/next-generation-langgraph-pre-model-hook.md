# LangGraph pre-model memory hook

`commontrace.integrations.langgraph.create_pre_model_hook(memory)` returns a
synchronous hook for LangGraph's prebuilt agent API:

```python
from langgraph.prebuilt import create_react_agent
from commontrace.integrations.langgraph import create_pre_model_hook

agent = create_react_agent(
    model,
    tools,
    pre_model_hook=create_pre_model_hook(memory),
)
```

The hook reads the latest human message, recalls through the supplied
`CausalMemory`, and uses `configurable.thread_id` as the causal occasion ID.
It returns only `llm_input_messages`; the retrieved block is sent to the model
for that call and stays out of the checkpointed `messages` history. Missing
thread identity and recall failures pass the original messages through without
memory. Tenant or user dimensions already needed by an existing retrieval
callable can be forwarded with `recall_kwargs(config)`.

The output follows the official `pre_model_hook` contract: LangGraph accepts a
callable or runnable that returns a state update with either `messages` or
`llm_input_messages`, and the latter supplies model input without updating
`messages`. The current source still implements this on
[`create_react_agent`](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py),
while marking that factory deprecated in favor of `langchain.agents.create_agent`
and its middleware hooks. This adapter targets the explicit LangGraph
prebuilt-hook contract named in the next-generation brief; integrations moving
to `create_agent` should use an equivalent `before_model` middleware adapter.

The implementation adapts the local Zep LangGraph integration's approach of
injecting a temporary system message through `llm_input_messages`, while
retaining CommonTrace's `CausalMemory` recall and measurement path. Mem0's local
guide instead demonstrates retrieval and prompt construction inside a graph
node; this helper avoids requiring users to replace their existing model node.
No new persistence or storage path is introduced.

Relevant local examples reviewed:

- Zep: `integrations/langgraph/python/src/zep_langgraph/hooks.py`
- Mem0: `docs/integrations/langgraph.mdx`
