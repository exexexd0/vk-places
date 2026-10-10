#!/bin/sh
set -eu

PROFILE="${OSRM_PROFILE:?OSRM_PROFILE is required}"
DATA_ROOT="${OSRM_DATA_DIR:-/data}"
PBF="$DATA_ROOT/ivanovo-oblast-latest.osm.pbf"
DIR="$DATA_ROOT/$PROFILE"
BASE="$DIR/ivanovo"

case "$PROFILE" in
  car|foot) ;;
  *)
    echo "Unsupported OSRM profile: $PROFILE" >&2
    exit 1
    ;;
esac

if [ ! -s "$PBF" ]; then
  echo "OSM extract is missing: $PBF" >&2
  exit 1
fi

if [ ! -s "$BASE.osrm.mldgr" ]; then
  rm -rf "$DIR"
  mkdir -p "$DIR"
  cp "$PBF" "$DIR/ivanovo.osm.pbf"
  osrm-extract -p "/opt/${PROFILE}.lua" "$DIR/ivanovo.osm.pbf"
  osrm-partition "$BASE.osrm"
  osrm-customize "$BASE.osrm"
fi

exec osrm-routed --algorithm mld --ip 0.0.0.0 --port 5000 "$BASE.osrm"
