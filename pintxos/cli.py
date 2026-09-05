"""Command-line entry point for the pintxos server."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import dotenv
import uvicorn

from pintxos.config import DEFAULTS


def main(argv: list[str] | None = None) -> None:
    # Load .env from the current working directory only (not the package directory or
    # any parent directory), before anything else, so a locally-run `pintxos` picks up
    # the same settings docker compose would via its own .env handling. Real environment
    # variables always win.
    found = os.path.isfile(".env")
    if found:
        dotenv.load_dotenv(".env", override=False)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)
    if found:
        logger.info("Loaded .env from %s", os.path.abspath(".env"))
    else:
        logger.info("No .env file found in %s; using environment only", os.getcwd())

    parser = argparse.ArgumentParser(prog="pintxos", description="Run the Pintxos server.")
    parser.add_argument("--host", default=None, help="Host/interface to bind (default: env PINTXOS_HOST or 127.0.0.1)")
    parser.add_argument("--port", default=None, help="Port to bind (default: env PINTXOS_PORT or 8000)")
    args = parser.parse_args(argv)

    # Host/port must be resolved before the app (and thus the database) is touched, so
    # read os.environ + DEFAULTS directly here instead of config.get_setting().
    host = args.host or os.environ.get("PINTXOS_HOST") or DEFAULTS["PINTXOS_HOST"]
    port_raw = args.port or os.environ.get("PINTXOS_PORT") or DEFAULTS["PINTXOS_PORT"]

    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError
    except (TypeError, ValueError):
        print(f"pintxos: invalid port {port_raw!r}; must be an integer between 1 and 65535", file=sys.stderr)
        sys.exit(2)

    uvicorn.run("pintxos.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
