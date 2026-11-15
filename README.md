# AI Browser Agent Service

A FastAPI service with a single-page chat frontend and a Groq-planned Playwright browser agent.

- `GET /` serves `manus-obtima.html`.
- `POST /execute` accepts `{"task":"..."}`. Groq turns the task into a strict-JSON action plan, and Playwright executes only the supported actions.
- The default Groq model is `openai/gpt-oss-20b`; set `GROQ_MODEL` to another active model if needed. Set `GROQ_API_KEY` as a secret environment variable in Render. Never commit the key or put it in the HTML.

The agent does not execute model-generated Python. It runs at most six allowlisted actions in a fresh anonymous browser context. It can navigate public HTTP(S) pages, follow ordinary links, fill non-sensitive text/search/URL fields without submitting them, and extract visible text. It blocks private/local network destinations, file downloads, form submissions, and risky purchase/delete/confirm-style link actions. File uploads are not supported.

## Run locally

```bash
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium
export GROQ_API_KEY=your-groq-api-key
export GROQ_MODEL=openai/gpt-oss-20b
uvicorn main:app --reload
```

Example request:

```bash
curl -X POST http://localhost:8000/execute \
  -H 'Content-Type: application/json' \
  -d '{"task":"Open https://example.com and extract the page title"}'
```

The browser runs on the service, not in the user's local browser, and each execution starts with a clean context. Configure API credentials in environment variables rather than committing them to source.
