#!/bin/sh
set -eu

PBF="/data/ivanovo-oblast-latest.osm.pbf"
URL="${OSRM_PBF_URL:?OSRM_PBF_URL is required}"

if [ -s "$PBF" ]; then
  exit 0
fi

wget -O "$PBF.partial" "$URL"
mv "$PBF.partial" "$PBF"
