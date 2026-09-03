#!/bin/sh
# MServer launcher — works in Termux and anywhere with python3.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "MServer needs python3. On Termux:  pkg install python" >&2
  exit 1
fi

# MServer requires Python 3.10+ (PEP 604 syntax). Fail with a sentence the
# user can act on rather than a traceback from deep inside an import.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  have=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
  echo "MServer needs Python 3.10 or newer — found $have." >&2
  echo "On Termux:  pkg update && pkg upgrade python" >&2
  exit 1
fi

exec python3 -m mserver "$@"
