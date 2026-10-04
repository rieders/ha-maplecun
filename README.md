# MapleCUN für Home Assistant

Custom Integration, die das **433-MHz-Modul eines [MapleCUN](https://wiki.fhem.de/wiki/MapleCUN)** (a-culfw, bis zu 4 CC1101-Funkmodule) direkt in Home Assistant bringt – ohne FHEM.

| Gerät | Funktion in HA |
|---|---|
| **Revolt NC-5462** Energiemesser | wird automatisch erkannt → Leistung, Energie (Energie-Dashboard), Spannung, Strom, Leistungsfaktor, Frequenz |
| **Intertechno** Funksteckdosen (Tristate) | Schalter |
| alle anderen 433-MHz-Telegramme | Diagnose-Sensor „Letzte Rohdaten“ (standardmäßig deaktiviert) |

## Voraussetzung: maplecun-splitproxy

Ein MapleCUN mit mehreren Funkmodulen spricht alle Module über **einen** TCP-Port an (Präfixe `*`, `**`, `***`). Der Docker-Container [boriswde/maplecun-splitproxy](https://hub.docker.com/r/boriswde/maplecun-splitproxy) gibt jedem Modul einen eigenen Port (1080–1083) und erlaubt mehrere Programme gleichzeitig (z. B. FHEM, wmbusmeters und Home Assistant).

Eine fertige `docker-compose.yaml` liegt unter [`extras/wmbusmeters`](extras/wmbusmeters).

> Hat dein MapleCUN nur ein Modul, kannst du die Integration auch direkt auf `IP-des-MapleCUN:2323` zeigen lassen.

## Installation

### HACS (empfohlen)

1. HACS → ⋮ → **Benutzerdefinierte Repositories** → `https://github.com/rieders/ha-maplecun`, Kategorie **Integration**
2. „MapleCUN“ installieren, Home Assistant neu starten

### Manuell

Ordner `custom_components/maplecun` nach `/config/custom_components/` kopieren und Home Assistant neu starten.

## Einrichtung

**Einstellungen → Geräte & Dienste → Integration hinzufügen → MapleCUN**

| Feld | Beispiel |
|---|---|
| Host | IP des Rechners mit dem Split-Proxy |
| Port | `1081` (Port des 433-MHz-Moduls) |
| Intertechno-Codes | `FFFF00FFFF, FFFF0F0FFF` (optional, 10-stellige Tristate-Codes wie in FHEM) |

Neue Intertechno-Steckdosen, abweichende Ein-/Aus-Codes (Standard: Ein `FF`, Aus `F0`) und das Entfernen von Steckdosen gibt es unter **Konfigurieren**.

Revolt-Geräte erscheinen automatisch, sobald sie funken. Nicht mehr benötigte Revolt-Geräte lassen sich auf der Geräteseite löschen.

### Codes aus FHEM übernehmen

In der FHEM-Befehlszeile:

```
list TYPE=IT DEF
```

Ausgabe z. B. `FFFF0F0FFF FF F0` → Code `FFFF0F0FFF`, Ein `FF`, Aus `F0`.

## Wireless M-Bus (Wasser-/Wärmezähler)

Wireless M-Bus wird (noch) nicht von der Integration selbst dekodiert. Bewährt hat sich [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters) am Split-Proxy. Dabei gibt es eine Hürde: a-culfw antwortet auf `brc` mit `OFF` statt `CMODE`, wmbusmeters bricht deshalb ab. [`extras/wmbusmeters/culbridge.py`](extras/wmbusmeters/culbridge.py) löst das – Anleitung siehe dort bzw. `docker-compose.yaml`.

## Technisches

* Verbindung per TCP (asyncio), automatischer Reconnect, Entitäten werden bei Verbindungsverlust „nicht verfügbar“
* Revolt-Dekodierung nach dem Aufbau von FHEM `19_Revolt.pm`, inkl. Plausibilitätsprüfung und Filter gegen Sprünge im Energiezähler
* Intertechno: a-culfw-Befehl `is<Code><Ein/Aus>`; Funk ist Einweg, der Zustand wird angenommen und über Neustarts wiederhergestellt

## Tests

```bash
pip install -r requirements_test.txt
pytest tests
```

## Danksagung

* [a-culfw](https://github.com/heliflieger/a-culfw) und die FHEM-Community
* [boriswde/maplecun-splitproxy](https://hub.docker.com/r/boriswde/maplecun-splitproxy)
* [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters)

## Lizenz

MIT
