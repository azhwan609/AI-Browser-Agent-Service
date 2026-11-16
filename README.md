# AI Browser Agent Service

A FastAPI service with a single-page chat frontend and a Groq-planned, safety-constrained Playwright browser agent.

- `GET /` serves `manus-obtima.html`.
- `GET /version` returns the deployment fingerprint `V2.0 - Chat Routing Active`; `GET /execute` remains a compatibility alias.
- `POST /execute` accepts `{"task":"..."}` and returns the JSON response format.
- `POST /execute/stream` accepts the same payload and streams named Server-Sent Events (`status`, `frame`, `complete`, `error`). Frames are Base64 JPEGs in `data:image/jpeg;base64,...` form. The live-browser panel opens only for browser tasks; ordinary chat returns text without a browser panel.
- The default Groq model is `openai/gpt-oss-20b`; set `GROQ_MODEL` to another active model if needed. Set `GROQ_API_KEY` as a secret environment variable in Render. Never commit the key or put it in the HTML.

The agent does not execute model-generated code. It runs at most six allowlisted actions in a fresh anonymous browser context. It can navigate public HTTP(S) pages, click visible links and standard form controls, fill enabled standard text/search/URL/email/password fields with values explicitly supplied by the user, and extract page titles, visible text, and bounded CSS-selected text. Standard form actions are allowed when explicitly requested; the planner is instructed not to perform purchases/payments, bookings, deletion, sending, or account changes unless specifically requested. Private/local destinations are blocked both before navigation and by a connection-time proxy that resolves the destination and pins the connection to a verified public IP. Downloads and file attachments are not supported.

## Run locally

```bash
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium
export GROQ_API_KEY=your-groq-api-key
export GROQ_MODEL=openai/gpt-oss-20b
uvicorn main:app --reload
```

For headed local live-browser screenshots, install the Xvfb system package (`xvfb`) and run under a virtual display; the application uses `pyvirtualdisplay` to start it for each live task. If Xvfb is unavailable, the live endpoint logs the condition and falls back to headless screenshots.

Run the automated tests with:

```bash
python -m unittest discover -s tests -v
```

Example request (legacy JSON endpoint):

```bash
curl -X POST http://localhost:8000/execute \
  -H 'Content-Type: application/json' \
  -d '{"task":"Open https://example.com and extract the page title"}'
```

The browser runs on the service, not in the user's local browser, and each execution starts with a clean context. Configure API credentials in environment variables rather than committing them to source.

## Render deployment

Render uses the repository's Dockerfile because Playwright Chromium needs OS-level packages. The image installs Chromium dependencies and Xvfb, then starts `uvicorn main:app` on Render's `PORT` (default 10000). The Blueprint health check uses `/version`, and `autoDeployTrigger: commit` deploys commits to `main`.
