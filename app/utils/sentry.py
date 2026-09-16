"""Sentry init options shared by the API process and the Celery worker."""


def integration_options() -> dict:
    """Keep Sentry's auto-enabled OpenAI integration from importing openai.

    Auto-enabled integrations import their target library at startup. The API
    never uses openai, but an incompatible release on disk (3.x, which needs
    httpx2 and breaks under pydantic<2) took the whole process down through
    this hook. The integration module itself imports openai at import time,
    so if even that fails, fall back to disabling auto-enabling entirely
    rather than crash.
    """
    try:
        from sentry_sdk.integrations.openai import OpenAIIntegration
    except Exception:
        return {"auto_enabling_integrations": False}
    return {"disabled_integrations": [OpenAIIntegration()]}
