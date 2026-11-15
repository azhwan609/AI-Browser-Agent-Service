"""FastAPI service that signs in to Sauce Demo using Playwright."""

import logging
import os

# Match the package-local browser location used by the Render build command.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


class ExecuteRequest(BaseModel):
    """Input accepted by the task execution endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    task: str = Field(min_length=1, description="Task to execute")


class ExecuteResponse(BaseModel):
    """Structured result returned by the task execution endpoint."""

    success: bool
    message: str
    task: str
    title: str


app = FastAPI(
    title="AI Browser Agent Service",
    description="FastAPI browser automation scaffold using Playwright.",
    version="0.2.1",
)


@app.post("/execute", response_model=ExecuteResponse)
async def execute(payload: ExecuteRequest) -> ExecuteResponse:
    """Log in to Sauce Demo and return the page title after successful login."""
    username = os.getenv("SAUCEDEMO_USERNAME")
    password = os.getenv("SAUCEDEMO_PASSWORD")
    if not username or not password:
        raise HTTPException(
            status_code=500,
            detail="Sauce Demo credentials are not configured on the service",
        )

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(
                    "https://www.saucedemo.com/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await page.locator("[data-test='username']").fill(username)
                await page.locator("[data-test='password']").fill(password)
                await page.locator("[data-test='login-button']").click()
                await page.locator(".inventory_list").wait_for(
                    state="visible",
                    timeout=20_000,
                )
                title = await page.title()
            finally:
                await browser.close()
    except PlaywrightError as exc:
        logger.exception("Playwright failed while executing the Sauce Demo login flow")
        raise HTTPException(
            status_code=502,
            detail="Browser automation failed while logging in to Sauce Demo",
        ) from exc

    return ExecuteResponse(
        success=True,
        message="Login successful",
        task=payload.task,
        title=title,
    )
