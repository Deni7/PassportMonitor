#!/bin/sh
set -eu
python -m alembic upgrade head
# Use ordinary headed Chromium on a virtual display. No stealth or challenge handling.
case "${DOCUMENT_BROWSER_HEADLESS:-false}" in
    true|True|TRUE|1) exec python -m app.main ;;
    *) exec xvfb-run -a python -m app.main ;;
esac
