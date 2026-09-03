#!/bin/sh
# MServer launcher — works in Termux and anywhere with python3.
cd "$(dirname "$0")" || exit 1
exec python3 -m mserver "$@"
