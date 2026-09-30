import logging
import os
import signal
import sys

from .bridge import run
from .config import Config, ConfigError


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO, which would log the API calls every poll
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Docker stops containers with SIGTERM; exit through run()'s cleanup to publish "offline"
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        config = Config.from_env()
    except ConfigError as error:
        sys.exit(str(error))

    try:
        run(config)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
