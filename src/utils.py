from __future__ import annotations

import logging


def configure_logging() -> None:
    # Streamlit Community Cloud's disk is ephemeral and not per-user, so logs go to
    # stdout (visible in Community Cloud's own log viewer) instead of a data/ file.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
