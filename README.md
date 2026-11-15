# AI Browser Agent Service

A FastAPI service that accepts a task and runs a browser login flow against the Sauce Demo sample site using Playwright. The current endpoint logs in with the configured standard demo account and returns the page title after login. The request's `task` is included in the response for traceability; it does not currently alter the fixed login flow.

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

The response includes `success`, `message`, `task`, and the browser-retrieved page `title`. Sauce Demo credentials are read from `SAUCEDEMO_USERNAME` and `SAUCEDEMO_PASSWORD`; configure them as environment variables rather than committing them to source. Render installs Chromium into Playwright's package-local browser directory during the build, and the app uses the same path at runtime.
