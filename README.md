# Maple Street Clinic front-desk agent

A text-chat agent for a small clinic front desk. It can:

- book, move and cancel appointments (tools backed by an in-memory store, seeded with two patients)
- answer opening hours questions

Its behaviour is defined in `instructions.md`, which `agent.py` loads at startup.

## Run

```
export LLM_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible endpoint
export LLM_API_KEY=...
export LLM_MODEL=...                            # a model that supports tool calling
python agent.py
```

No third-party packages are needed (Python 3.10+).

The agent serves `POST /v1/chat/completions` (OpenAI chat format, full message history in each
request) and `GET /health`.

## Environment variables

| Name | Required | Purpose |
| --- | --- | --- |
| `LLM_BASE_URL` | yes | Base URL of the OpenAI-compatible API, ending in `/v1` |
| `LLM_API_KEY` | yes | API key for that endpoint |
| `LLM_MODEL` | yes | Model name |
| `PORT` | no | Listen port, default `8080` |

## Seeded patients (for manual testing)

Priya Sharma, phone 5550101, and Daniel Okoye, phone 5550102, each have one upcoming appointment.

Each reply also lists the tools it ran in `completed_tools`: one object per call with `id`, `name`, `arguments`, `result` and `error`.
