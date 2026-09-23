# Local install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
# Running outside Docker? Ollama is on localhost, not host.docker.internal:
#   LLM_PROVIDER=ollama
#   OLLAMA_BASE_URL=http://localhost:11434/v1
python -m app.main
```

Open http://127.0.0.1:7075/docs

With `DC_API_KEY` set, the Data Commons `dc_` tools are registered — see
**Adding a tool** in the README for your own.
