#!/bin/sh
set -eu
umask 077
exec /usr/local/bin/python3 -B /opt/acp/runtime.py "$(basename "$0")" "$@"
