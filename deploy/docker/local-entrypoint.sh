#!/bin/sh
# Initialize the store on first start, then run the requested unimem command.
set -eu
root="${UNIMEM_ROOT:-/data}"
if [ ! -d "$root/memory" ]; then
    unimem init --dest "$root" >/dev/null
fi
case "${1:-}" in
    gateway|serve) exec unimem "$@" --dest "$root" ;;
    *) exec unimem "$@" ;;
esac
