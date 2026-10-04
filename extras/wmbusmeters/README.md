# wmbusmeters am MapleCUN

`culbridge.py` stellt ein MapleCUN-Funkmodul (über den Split-Proxy) als virtuelle serielle Schnittstelle bereit und beantwortet die Moduswahl von wmbusmeters (`brc` → `CMODE`) selbst – a-culfw meldet hier fälschlich `OFF`, wodurch wmbusmeters sonst abbricht.

## Aufbau

```
wmbusmeters/            # neben docker-compose.yaml
├── start.sh
├── culbridge.py
└── etc/
    ├── wmbusmeters.conf          # aus wmbusmeters.conf.example
    └── wmbusmeters.d/
        └── wasserzaehler         # name=…, driver=…, id=…, key=NOKEY
```

1. Port des Wireless-M-Bus-Moduls in `start.sh` eintragen (Standard 1080).
2. MQTT-Daten in `wmbusmeters.conf` setzen.
3. `docker compose up -d` und `docker logs -f wmbusmeters`.
