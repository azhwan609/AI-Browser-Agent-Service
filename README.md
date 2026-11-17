# AI Browser Agent Service

A FastAPI service with a single-page chat frontend and a Groq-planned, safety-constrained Playwright browser agent.

- `GET /` serves `manus-obtima.html`.
- `GET /profiles` serves the same frontend with its account-profile screen selected.
- `GET /version` returns the deployment fingerprint `V2.0 - Chat Routing Active`; `GET /execute` remains a compatibility alias.
- `POST /execute` accepts `{"task":"..."}` and returns the JSON response format.
- `POST /execute/stream` accepts the same payload and streams named Server-Sent Events (`status`, `frame`, `complete`, `error`). Frames are Base64 JPEGs in `data:image/jpeg;base64,...` form. The live-browser panel opens only for browser tasks; ordinary chat returns text without a browser panel.
- The default Groq model is `openai/gpt-oss-20b`; set `GROQ_MODEL` to another active model if needed. Set `GROQ_API_KEY` as a secret environment variable in Render. Never commit the key or put it in the HTML.

## Account profiles

The metallic top navigation includes an account-profile screen with Google and GitHub provider cards. This is a UI-only preview: OAuth is not configured, the Connect buttons only explain that limitation, and no credentials or account data are collected or stored. Connecting an identity provider would require a separate OAuth setup and persistent datastore; provider OAuth does not create a universal login session for unrelated websites.

The agent does not execute model-generated code. It runs at most six allowlisted actions in a fresh anonymous browser context. It can navigate public HTTP(S) pages, click visible links and standard form controls, fill enabled standard text/search/URL/email/password fields with values explicitly supplied by the user, and extract page titles, visible text, and bounded CSS-selected text. Standard form actions are allowed when explicitly requested; the planner is instructed not to perform purchases/payments, bookings, deletion, sending, or account changes unless specifically requested. Private/local destinations are blocked both before navigation and by a connection-time proxy that resolves the destination and pins the connection to a verified public IP. Downloads and file attachments are not supported.

## Run locally

```bash
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=0 playwright install chromium
export GROQ_API_KEY=your-groq-api-key
export GROQ_MODEL=openai/gpt-oss-20b
uvicorn main:app --reload
```

Browser tasks run headed under an isolated Xvfb display. The live endpoint streams JPEG screenshots to the UI as Server-Sent Events while the task runs. Install the Xvfb system package (`xvfb`) locally; if the display cannot start, the task returns a friendly error instead of silently switching to headless mode.

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

Render uses the versioned official Playwright Python image `mcr.microsoft.com/playwright/python:v1.63.0-noble`. It provides Chromium and the browser's Linux dependencies; the Dockerfile also installs `xvfb` and `xauth` for headed browser sessions. `requirements.txt` pins the Python Playwright package to the same `1.63.0` release, and the browser path is `/ms-playwright`; the Dockerfile does not rerun `playwright install --with-deps`. The service starts `uvicorn main:app` on Render's `PORT` (default 10000). The Blueprint health check uses `/version`, and `autoDeployTrigger: commit` deploys commits to `main`.

## Identity Vault and persistent session state

The repository now includes `identity_vault.py`, a file-backed vault for isolated profile metadata and encrypted Playwright `storage_state` snapshots. The vault never accepts or stores a Google password, OTP, CAPTCHA response, cookie in the frontend, or access token in API responses. A profile is scoped by the `X-Profile-Owner` namespace and every management request also requires `X-Profile-Admin-Key`. This is a service-level control boundary; a real multi-user deployment should put the API behind an authenticated user identity provider before exposing it publicly.

The management API is: `POST /api/profiles`, `GET /api/profiles`, `GET/PATCH/DELETE /api/profiles/{profile_id}`, `POST /api/profiles/{profile_id}/default`, `POST /api/profiles/{profile_id}/disconnect`, and `POST /api/profiles/{profile_id}/auth-status`. Browser execution accepts an optional `profile_id`; when both profile headers are present, it restores the encrypted Playwright storage state and saves the updated state encrypted after execution. A saved state is reported as `session_saved`, not as authenticated. Marking it `active` requires an explicit human verification request and a saved state.

Set `IDENTITY_VAULT_KEY`, `PROFILE_ADMIN_KEY`, and `IDENTITY_VAULT_DIR` as Render secrets/configuration. `IDENTITY_VAULT_KEY` must be a stable Fernet key across restarts. The free Render filesystem is ephemeral and the deployed `render.yaml` now fails closed with `REQUIRE_PERSISTENT_STORAGE=true` until a real mount exists. `render.persistent.yaml` is the paid Render Blueprint with a 1 GB disk at `/var/data`; apply it only after approving the paid plan, or use an external encrypted storage service. The application never silently falls back to ephemeral storage.

Google connection remains browser-mediated and human-controlled. No Google OAuth client is embedded yet, and the current service does not claim a Google account is connected merely because a page redirected. MFA, CAPTCHA, consent, and account verification must be completed by the user in an approved browser interaction before a profile can be marked active.
