# AI Browser Agent Service

A FastAPI service with a single-page chat-style frontend and a Playwright flow for the Sauce Demo sample site.

- `GET /` serves `manus-obtima.html`.
- `POST /execute` accepts `{"task":"..."}`, logs in to Sauce Demo using the credentials stored in Render environment variables, and returns the page title.
- The frontend sends text tasks to `/execute` and displays the backend result. File uploads are not yet supported by the backend.

The current browser flow is fixed to the Sauce Demo login; the submitted task is retained in the response for traceability and does not change the flow.

## Run locally

```bash
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium
export SAUCEDEMO_USERNAME=standard_user
export SAUCEDEMO_PASSWORD=secret_sauce
uvicorn main:app --reload
```

Example request:

```bash
curl -X POST http://localhost:8000/execute \
  -H 'Content-Type: application/json' \
  -d '{"task":"Test deployment"}'
```

Sauce Demo credentials are read from `SAUCEDEMO_USERNAME` and `SAUCEDEMO_PASSWORD`; configure them as environment variables rather than committing them to source. Render installs Chromium into Playwright's package-local browser directory during the build, and the app uses the same path at runtime.
