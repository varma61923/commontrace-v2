#!/bin/sh
# Initialize the store on first start, then run the requested commontrace command.
set -eu
root="${COMMONTRACE_ROOT:-/data}"
if [ ! -d "$root/memory" ]; then
    commontrace init --dest "$root" >/dev/null
fi
case "${1:-}" in
    gateway|serve) exec commontrace "$@" --dest "$root" ;;
    *) exec commontrace "$@" ;;
esac
