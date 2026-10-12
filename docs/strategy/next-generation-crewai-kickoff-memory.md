# CrewAI kickoff memory callbacks

`Middleware` now provides a synchronous before/after kickoff pair for CrewAI.
The callbacks use the existing scoped `MemoryClient` turn and SessionEnd paths;
they do not create a framework-specific store or import CrewAI.

```python
from commontrace.client import MemoryClient
from commontrace.framework_middleware import Middleware

memory = MemoryClient("./memory", agent_id="research-crew", context={"user": "alice"})
middleware = Middleware(memory, session_id="run-42")

# Add these bound callbacks to Crew's before_kickoff_callbacks and
# after_kickoff_callbacks constructor parameters.
before = middleware.crewai_before_kickoff
after = middleware.crewai_after_kickoff
```

The before callback searches the kickoff inputs and adds the retrieved block as
`commontrace_memory`. Reference `{commontrace_memory}` in a task description or
prompt to make CrewAI interpolate it. That field is reserved: an old injected
value is removed before retrieval, and the caller's input dictionary is not
mutated. Retrieval errors or non-JSON kickoff inputs skip injection and allow
the crew to continue.

The after callback captures the output's `raw` text (or its string form),
bounded to 20,000 characters, through the existing `SessionEnd` event path. It
returns the exact output object CrewAI supplied. Its default `succeeded=True`
means kickoff returned normally; it does not judge application-level quality.
Call `crewai_after_kickoff(output, succeeded=False)` when a caller has an
explicit failed outcome to record. Capture errors are debug-logged and do not
replace or fail the result.

This shape adapts the before/after memory lifecycle rather than Zep's backend:
the sibling competitor integration at
`/root/Test/zep/integrations/crewai/python/src/zep_crewai/memory.py` separates
`search()` and `save()`, and its
`/root/Test/zep/integrations/crewai/python/tests/test_basic.py` and
`/root/Test/zep/integrations/crewai/python/tests/test_context_builder.py`
verify that storage/search failures degrade safely.
CommonTrace uses the same fail-open boundary with its own `MemoryClient` and
`Middleware`, keeping scope, capture, and outcome handling in the existing
runtime. No Zep client, cloud service, or CrewAI package is required to import
or use the base CommonTrace package.

The adapter callbacks are synchronous. CrewAI's `kickoff_async()` currently
runs the synchronous `kickoff()` in a worker thread, so this pair can be used
there. Its native `akickoff()` path calls `prepare_kickoff()` synchronously;
these callbacks can block that event loop, so use them only when that blocking
behavior is acceptable. The current adapter does not provide async callbacks.
