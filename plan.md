# UI activation plan

The existing Render service already serves the metallic dark single-page UI and connects to the Groq-planned Playwright engine. Preserve that visual system and backend safety model. Activate the in-app Activity and Library controls, and forward the selected UI mode metadata to the existing same-origin streaming endpoint without exposing credentials or changing browser safety limits.

## Structure

- `manus-obtima.html`: existing UI, controls, streaming adapter, and new session drawers.
- `main.py`: existing FastAPI, Groq planning/chat, and Playwright execution contract.
- `tests/`: regression coverage for the frontend/backend contract.

## Design

Use the existing dark metallic/retro control-surface aesthetic: compact chrome, high-contrast labels, amber/green status accents, and lightweight drawers that reuse the current palette rather than introducing a separate visual language. Interactions should be immediate and reversible: controls visibly reflect state, Activity is session-local, and Library reports selected files without persisting credentials or file contents.
