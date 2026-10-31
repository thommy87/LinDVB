#!/bin/sh
export PYTHONPATH=/app/share/lindvb${PYTHONPATH:+:$PYTHONPATH}
exec python3 -m lindvb "$@"
