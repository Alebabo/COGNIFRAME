#!/usr/bin/env sh
set -eu

videos_dir="."
host_address="127.0.0.1"
port="8000"
template_source=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --videos-dir) videos_dir="$2"; shift 2 ;;
    --host) host_address="$2"; shift 2 ;;
    --port) port="$2"; shift 2 ;;
    --template-source) template_source="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

set -- uv run --no-env-file pitchcraft --videos-dir "$videos_dir" serve \
  --host "$host_address" --port "$port"
if [ -n "$template_source" ]; then
  set -- "$@" --template-source "$template_source"
fi
exec "$@"
