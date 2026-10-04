#!/bin/sh
# Startskript für den wmbusmeters-Container: culbridge statt socat
command -v python3 >/dev/null || apk add --no-cache python3 || exit 1
python3 /wmbusmeters_data/culbridge.py maplecun-splitproxy 1080 c /dev/ttyCUL_C &
while [ ! -e /dev/ttyCUL_C ]; do sleep 0.2; done
echo "CUL-Schnittstelle bereit"
exec sh /wmbusmeters/docker-entrypoint.sh
