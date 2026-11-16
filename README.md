# AI Browser Agent Service

A FastAPI service with a single-page chat frontend and a Groq-planned, safety-constrained Playwright browser agent.

- `GET /` serves `manus-obtima.html`.
- `POST /execute` accepts `{"task":"..."}` and returns the existing JSON response format.
- `POST /execute/stream` accepts the same payload and streams named Server-Sent Events (`status`, `frame`, `complete`, `error`). Frames are Base64 JPEGs in `data:image/jpeg;base64,...` form. The browser panel opens when a task starts; the final answer is still shown in chat.
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

For headed live browser screenshots, install the Xvfb system package (`xvfb`) and run under a virtual display; the application uses `pyvirtualdisplay` to start it for each live task. If Xvfb is unavailable, the live endpoint logs the condition and falls back to headless screenshots. `render.yaml` includes Xvfb installation in its build command.

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

## Render deployment note

The current Render service uses a Build Command set directly in the service dashboard. A `render.yaml` edit does not necessarily overwrite that existing setting. To guarantee headed Chromium, update the service's dashboard Build Command to:

```bash
apt-get update && apt-get install -y xvfb && PLAYWRIGHT_BROWSERS_PATH=0 pip install -r requirements.txt && PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium
```

The `main` branch is configured for automatic deployment after a push. Confirm the deployment reaches `live` and test the public service before treating a push as a successful deployment.
