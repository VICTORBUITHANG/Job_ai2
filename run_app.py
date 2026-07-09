# File: /Users/victorbui/AI/Job_ai2/run_app.py
# Default interpreter: /Users/victorbui/venvs/ai312/bin/python
from job_ai2_agent.config import load_settings


def main() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "job_ai2_agent.web_app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
