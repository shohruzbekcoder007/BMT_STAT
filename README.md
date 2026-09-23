# Hermes host agent — starter

A minimal, production-shaped FastAPI service around a **Hermes host agent**:
conversation, multi-turn session memory, and a tool-calling loop. No domain
logic — add your own tools and build from here.

## Architecture

```
Gateway (Open WebUI, …) → POST /v1/chat
    → Hermes host agent  (system prompt + session history)
        └─ tools from agents/hermes_host.py::_host_langchain_tools()
```

The real Hermes framework is a hard requirement. Its build backend refuses pip
wheel builds by design, so the image clones the source to `/opt/hermes-agent`
and puts it on `PYTHONPATH` rather than installing it. The build then verifies
the import and **fails** if it is not usable — an image never ships silently
without Hermes.

Two backends, chosen automatically at startup:

| Backend | When | What |
|---|---|---|
| `hermes` | normal operation | real Hermes `AIAgent` — plugins, toolsets, its own conversation loop |
| `hermes_lite` | safety net if the above cannot start | LangGraph `create_react_agent` with the same design |

Hermes resolves its own provider credentials from the environment — no
interactive `hermes login` or `hermes setup` step, so deployment stays a
single command. See **LLM provider** below for which variables it reads.

## LLM provider

`LLM_PROVIDER` is the only switch. It picks one block of `.env`; the other
block sits there untouched, so an OpenAI key and a local server can coexist
and neither leaks into the other.

```env
LLM_PROVIDER=openai            # openai | ollama | vllm | lmstudio

# 1: OpenAI
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4.1
HERMES_TASK_MODEL=gpt-4.1-mini

# 2: local OpenAI-compatible server (Ollama / vLLM / LM Studio)
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_MODEL=qwen3.8:27b
OLLAMA_TASK_MODEL=qwen3:8b
```

| `LLM_PROVIDER` | Key | Endpoint | Model | Key required |
|---|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | `LLM_MODEL` | yes |
| `ollama` | `OLLAMA_API_KEY` | `OLLAMA_BASE_URL` | `OLLAMA_MODEL` | no |
| `vllm` | `OLLAMA_API_KEY` | `OLLAMA_BASE_URL` | `OLLAMA_MODEL` | no |
| `lmstudio` | `OLLAMA_API_KEY` | `OLLAMA_BASE_URL` | `OLLAMA_MODEL` | no |

The three local providers share one env block on purpose — point
`OLLAMA_BASE_URL` at whichever server is running. They differ only in the
provider profile handed to Hermes, and that profile is not cosmetic: the
`ollama` one sends `think=false`, detects `num_ctx` and lifts the `max_tokens`
floor. Without it Ollama truncates every reply at its internal
`num_predict=128` default.

`HERMES_INFERENCE_PROVIDER` follows `LLM_PROVIDER` automatically; set it only
to override. From Docker, the host machine's Ollama is reachable at
`host.docker.internal` — compose maps it for Linux hosts too.

The model must support tool calling, since the host agent is a tool-calling
loop (`ollama show <model>` lists `tools` under Capabilities).

### Task model

A chat UI runs small jobs behind the scenes — Open WebUI generates chat
titles, tags and follow-up suggestions by sending a prompt marked `### Task:`
through the normal chat route. When `HERMES_TASK_MODEL` (OpenAI) or
`OLLAMA_TASK_MODEL` (local) is set, those go to that model in a single call:
no tools, no history, no memory writes. Everything else reaches the full
agent. If the task model errors, the request falls through to the main agent
rather than failing.

Unset the variable, or set `HERMES_TASK_ROUTING=false`, and the main model
answers them as before.

## Hermes toolsets

`HERMES_ENABLED_TOOLSETS` selects which of Hermes' 59 toolsets the host agent
gets. The default enables persistence and self-improvement, and nothing that
reaches outside the container:

| Toolset | Tools | What it gives the agent |
|---|---|---|
| `memory` | `memory` | durable facts, re-injected into every later turn |
| `session_search` | `session_search` | recall and summarize past conversations |
| `skills` | `skill_manage`, `skill_view`, `skills_list` | write and revise its own skill documents |
| `todo` | `todo` | plan multi-step work |

Everything Hermes learns lives under `HERMES_HOME` — `memories/`, `skills/`,
`sessions/`, `state.db`, `SOUL.md` — kept in the `unstat-hermes-home` named
volume so a redeploy does not wipe it. `config/hermes_config.yaml` is copied in
only when the volume has no `config.yaml` yet; delete the volume to re-seed it.

### Prompt-injection guards

Anything the model reads — a user message, a Data Commons result, a memory it
wrote earlier — can steer which tools it calls. So the boundary is the tool
set, enforced in `agents/security.py`:

- **Toolset allowlist.** Only `memory`, `session_search`, `skills`, `todo`,
  `clarify` and `datacommons` are allowed. Anything else — `terminal`, `file`,
  `code_execution`, `browser`, `web`, `delegation`, bundles such as `coding`
  that pull those in — stops the service at startup unless
  `ALLOW_UNSAFE_TOOLSETS=true`. It is an allowlist because a denylist misses
  bundles and plugin toolsets.
- **`skills.inline_shell: false`, pinned.** With it on, `skill_view` runs
  `` !`cmd` `` snippets in a skill, and the `skills` toolset lets the agent
  write that skill itself. It is forced off in the shared config and in every
  user profile's `config.yaml` as the profile is opened — existing volumes
  included.
- **Root-owned code.** In the image `/app` belongs to root; the service
  (`appuser`) can write only `logs/`, `data/` and the Hermes homes. It cannot
  rewrite its code or prompts, or drop a plugin into `/app/.hermes/plugins`.

## Data Commons MCP

World statistics (countries, regions, indicators, time series) come from the
official Data Commons MCP server, `https://api.datacommons.org/mcp`. Set
`DC_API_KEY` (free, https://apikeys.datacommons.org) and the host gets six
tools, prefixed `dc_`:

`dc_search_indicators`, `dc_search_child_indicators`,
`dc_get_variable_metadata`, `dc_get_observations`,
`dc_get_child_observations`, `dc_get_multi_entity_observations`.

The MCP client runs in-process (`agents/datacommons_mcp.py`, stdlib only) and
`agents/datacommons_tools.py` hands the same tools to both backends:

| Backend | How the tools arrive |
|---|---|
| `hermes` | registered in Hermes' global tool registry, toolset `datacommons` — appended to `HERMES_ENABLED_TOOLSETS` automatically, visible to every user profile |
| `hermes_lite` | LangChain `StructuredTool`s from `_host_langchain_tools()` |

Hermes' own `mcp_servers:` config is not used: it is only discovered from
Hermes' CLI/gateway entry points, and it is read per `HERMES_HOME`, which this
service overrides per user.

The server's main playbook (`data-commons-researcher`) is appended to the
system prompt — its tool descriptions require reading it before the first
call. `DATACOMMONS_PLAYBOOK=false` turns that off.

Startup never fails on Data Commons. If the server is unreachable the host
runs without the `dc_` tools, `/ready` reports it under `datacommons.error`,
and a later chat request retries (at most every `DATACOMMONS_RETRY_SECONDS`).

## Layout

```
agents/
  hermes_host.py    host agent: sessions, backends, tool loop
  datacommons_mcp.py    MCP client (Streamable HTTP, stdlib only)
  datacommons_tools.py  Data Commons tools for both backends
app/
  api.py            FastAPI routes
  main.py           process entrypoint (uvicorn)
  logging_setup.py  JSON logging
config/
  hermes_config.yaml  Hermes profile (plugins/toolsets off by default)
  logging.yaml
prompts/
  hermes_coordinator.md  host system prompt
scripts/
  start.sh          container entrypoint
  healthcheck.sh
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness — never touches the LLM |
| GET | `/ready` | host readiness; `503` when not ready |
| GET | `/v1/info` | backend, provider, model, task model, registered tools |
| POST | `/v1/chat` | chat; `{"message": "...", "session_id": "...", "reset_session": false}` |
| GET | `/docs` | OpenAPI UI |

Set `API_BEARER_TOKEN` to require `Authorization: Bearer …` on `/v1/chat`.

## Run

Local: see [install_local.md](install_local.md) · Docker: see [install.md](install.md)

```bash
# Docker: host port is HOST_PORT (7075 by default). Running locally: APP_PORT (7075).
curl -s localhost:7075/v1/chat -H 'content-type: application/json' \
  -d '{"message":"salom"}'
```

## Adding a tool

A tool has to reach both backends, and they take tools from different places.
`agents/datacommons_tools.py` is the pattern:

1. **hermes** — register it in Hermes' registry, `tools.registry.registry.register(...)`,
   under a toolset of your own; the host calls that before the backend is built
   (see `_load_datacommons()`).
2. **hermes_lite** — return it as a LangChain tool from
   `agents/hermes_host.py` → `_host_langchain_tools()`.
3. Add the toolset to `SAFE_TOOLSETS` in `agents/security.py` only if it cannot
   run commands, read files or reach arbitrary URLs.
4. Describe it in `prompts/hermes_coordinator.md` under **Tools**.

Adding it to `_host_langchain_tools()` alone is not enough: the hermes backend
never reads that list.

## Configuration

All via environment (`.env`, see `.env.example`).

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` \| `ollama` \| `vllm` \| `lmstudio` |
| `OPENAI_API_KEY` | — | required when `LLM_PROVIDER=openai` |
| `OPENAI_BASE_URL` | OpenAI | any compatible gateway |
| `LLM_MODEL` | `gpt-4.1` | also `HERMES_MODEL`, `OPENAI_MODEL` |
| `HERMES_TASK_MODEL` | — | small model for `### Task:` prompts |
| `OLLAMA_BASE_URL` | `localhost:11434/v1` | local server; used by all three local providers |
| `OLLAMA_MODEL` | `qwen3:8b` | must support tool calling |
| `OLLAMA_TASK_MODEL` | — | small model for `### Task:` prompts |
| `OLLAMA_API_KEY` | — | only behind an authenticating proxy |
| `HERMES_TASK_ROUTING` | `true` | `false` = never route to the task model |
| `HERMES_INFERENCE_PROVIDER` | from `LLM_PROVIDER` | override only |
| `HERMES_SYSTEM_PROMPT_PATH` | `prompts/hermes_coordinator.md` | host prompt |
| `HERMES_ENABLED_TOOLSETS` | `memory,session_search,skills,todo` | comma-separated Hermes toolsets |
| `HERMES_MAX_ITERATIONS` | `12` | tool-loop cap |
| `HERMES_SESSION_HISTORY_LIMIT` | `6` | turns kept per session |
| `HERMES_SKIP_MEMORY` | `false` | `true` = stateless |
| `HERMES_REASONING_ENABLED` | `false` | keep `false` on gpt-4* |
| `API_BEARER_TOKEN` | — | unset = no auth |
| `CORS_ORIGINS` | `*` | comma-separated |

Sessions are in-process and per-worker: `API_WORKERS>1` will split them.
