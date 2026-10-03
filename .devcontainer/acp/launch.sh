#!/bin/sh
set -eu
umask 077
exec /usr/local/bin/python3 -B "$(dirname "$(readlink -f "$0")")/runtime.py" "$(basename "$0")" "$@"
