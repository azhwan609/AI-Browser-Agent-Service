"""Minimal FastAPI service with a replaceable task-execution boundary."""

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field


class ExecuteRequest(BaseModel):
    """Input accepted by the task execution endpoint."""

    model_config = ConfigDict(str_strip_whitespace=True)

    task: str = Field(min_length=1, description="Task to execute")


class ExecuteResponse(BaseModel):
    """Structured result returned by the task execution endpoint."""

    success: bool
    message: str
    task: str


app = FastAPI(
    title="AI Browser Agent Service",
    description="HTTP service scaffold for future browser-agent integrations.",
    version="0.1.0",
)


@app.post("/execute", response_model=ExecuteResponse)
async def execute(payload: ExecuteRequest) -> ExecuteResponse:
    """Accept a task and return a mock result.

    Replace the mock below with an async execution function when integrating
    Playwright/Groq. Keeping that logic behind this endpoint makes the HTTP
    contract independent of the eventual agent implementation.
    """
    # TODO: call the future Playwright/Groq task runner here.
    return ExecuteResponse(
        success=True,
        message="Mock execution successful",
        task=payload.task,
    )
