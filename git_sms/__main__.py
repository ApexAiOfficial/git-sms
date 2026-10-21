"""Run the server: `python -m git_sms` (configuration comes from the environment)."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(
        "git_sms.app:create_app",
        factory=True,
        host=os.getenv("GIT_SMS_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
