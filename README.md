# Evalon

Evalon is local observability for Python agents. It records traces, spans,
events, metrics, inputs, outputs, and errors in SQLite, then lets you inspect
them in the built-in terminal UI.

## First trace

Create a UV project and install Evalon:

```bash
uv init evalon-quickstart
cd evalon-quickstart
uv add evalon
```

Put this in `main.py`:

```python
import evalon

evalon.init("my-agent")


@evalon.observe
def greet(name: str) -> str:
    return f"Hello, {name}!"


print(greet("Ada"))
```

Run it, then open the trace viewer:

```bash
uv run python main.py
uv run evalon
```

`evalon.init("my-agent")` and the `evalon` command use the same default
database: `~/.evalon/evalon-runs.sqlite`. The `@evalon.observe` decorator
creates a trace, records the function arguments and return value, and records
any exception. It works with both synchronous and asynchronous functions.

To use another database, pass the same path when recording and viewing:

```python
evalon.init("my-agent", output="runs.sqlite")
```

```bash
uv run evalon runs.sqlite
```

You can also set `EVALON_DB` instead of passing a path.

## Provider tracing

Install the optional provider SDKs:

```bash
uv add "evalon[providers]"
```

Create an instrumented client and use it in your agent:

```python
import evalon

evalon.init("support-agent")
client = evalon.openai()


@evalon.observe
def answer(question: str) -> str:
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=question,
    )
    return response.output_text
```

The provider wrappers preserve the official SDK interface while recording
requests, responses, token usage, latency, errors, and cost when available.
Supported constructors are:

```python
openai_client = evalon.openai()
async_openai_client = evalon.openai(async_client=True)
anthropic_client = evalon.anthropic()
openrouter_client = evalon.openrouter()
groq_client = evalon.groq()
mistral_client = evalon.mistral()
deepseek_client = evalon.deepseek()
together_client = evalon.together()
xai_client = evalon.xai()
gemini_client = evalon.gemini()
```

`groq`, `mistral`, `deepseek`, `together`, and `xai` are OpenAI-compatible
providers — they return the same instrumented client surface as `openai()` and
accept the same keyword arguments (plus `async_client=True`). `gemini()` wraps
the Google GenAI SDK and exposes `client.models.generate_content` and
`client.models.stream_generate_content`:

```python
evalon.init("support-agent")
client = evalon.gemini()

@evalon.observe
def answer(question: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=question,
    )
    return response.text
```

When a wrapped provider is called inside `observe` or `trace`, the LLM call is
recorded as a span in that trace. When it is called outside an active trace,
Evalon automatically creates a root trace for the call, so instrumentation
does not silently disappear.

### Wrap an existing client

You do not have to let Evalon construct the SDK client:

```python
from openai import OpenAI
import evalon

evalon.init("support-agent")

sdk_client = OpenAI(timeout=30)
client = evalon.openai(sdk_client)
```

The same pattern works with `evalon.anthropic(existing_client)`,
`evalon.openrouter(existing_client)`, the other OpenAI-compatible constructors,
and `evalon.gemini(existing_client)`. If your application already installs its
provider SDK, the base `evalon` package is enough. The `providers` extra is only
needed when you want Evalon to install the OpenAI, Anthropic, and Google GenAI
SDKs for you.

## Advanced tracing

Use explicit trace and tool APIs when you need control over trace names,
expected values, metadata, or trace boundaries:

```python
import evalon

evalon.init("support-agent")


@evalon.tool
async def lookup_order(order_id: str) -> dict[str, str]:
    return {"order_id": order_id, "status": "delivered"}


async def run_case(question: str) -> str:
    async with evalon.trace(
        "refund-policy-case",
        input=question,
        expected="Explain the refund policy",
        metadata={"suite": "refunds"},
    ):
        result = await agent.run(question)
        evalon.record_output(result)
        return result
```

`evalon.observe` is the default for complete agent runs. Use `evalon.trace`
when the trace boundary or recorded output needs to be managed manually.
Decorated tools automatically become child spans when called within either
kind of trace.

## Inspect stored traces

`evalon.init` returns the active client, including its local storage:

```python
client = evalon.init("support-agent")

traces = client.storage.query(project="support-agent", status="success")
trace = client.storage.get_trace("trace_abc123")
error_count = client.storage.count(status="error")
```
