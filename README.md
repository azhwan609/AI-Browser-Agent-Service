# AI Browser Agent Service

A small FastAPI service scaffold for a future browser-agent integration. The `POST /execute` endpoint currently returns a mock success response; it does not launch a browser or call an LLM yet.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Example request:

```bash
curl -X POST http://localhost:8000/execute \
  -H 'Content-Type: application/json' \
  -d '{"task":"Test deployment"}'
```

Expected response:

```json
{"success":true,"message":"Mock execution successful","task":"Test deployment"}
```

The endpoint's mock body in `main.py` is the intended integration point for an async Playwright/Groq task runner. No credentials or environment variables are required for the scaffold.
