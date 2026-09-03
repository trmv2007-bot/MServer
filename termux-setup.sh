#!/data/data/com.termux/files/usr/bin/bash
# One-shot Termux setup for MServer.
set -e
echo "==> MServer setup (Termux)"
pkg update -y
pkg install -y python
pkg install -y git || true
echo "==> Done!"
echo
echo "Start MServer with:   bash run.sh"
echo "With dashboard:       bash run.sh --web"
echo "Tip: run 'termux-wake-lock' before long agent sessions."
