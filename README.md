# MapleCUN für Home Assistant

Custom Integration, die alle Funkmodule eines **[MapleCUN](https://wiki.fhem.de/wiki/MapleCUN)** (a-culfw, bis zu 4 CC1101-Module) direkt in Home Assistant bringt – ohne FHEM, ohne wmbusmeters.

| Aufgabe eines Moduls | Geräte in Home Assistant |
|---|---|
| **Wireless M-Bus C / T / S** | Wasser-, Wärme-, Gaszähler, Heizkostenverteiler (OMS): Zählerstand, Stichtagswert, Stichtag, Signalstärke … – auch über Repeater empfangene und AES-verschlüsselte Zähler (Modus 5) |
| **433 MHz** | Revolt NC-5462 Energiemesser (automatisch erkannt), Intertechno-Steckdosen (Tristate) |
| **Nur überwachen** | z. B. ein HomeMatic-/MAX!-Modul, das weiter von FHEM bedient wird – nur Verbindungsstatus |

Jedes Modul bekommt einen Verbindungs-Sensor, dazu gibt es einen Diagnose-Sensor **„Empfangene Zähler“** mit allen gehörten Wireless-M-Bus-IDs.

## Voraussetzung: maplecun-splitproxy

Ein MapleCUN spricht alle Module über **einen** TCP-Port an (Präfixe `*`, `**`, `***`). Der Container [boriswde/maplecun-splitproxy](https://hub.docker.com/r/boriswde/maplecun-splitproxy) gibt jedem Modul einen eigenen Port (1080–1083) und erlaubt mehrere Programme gleichzeitig (FHEM und Home Assistant parallel).

```yaml
services:
  maplecun-splitproxy:
    image: boriswde/maplecun-splitproxy:latest
    restart: unless-stopped
    ports: ["1080:1080", "1081:1081", "1082:1082", "1083:1083"]
    environment:
      - MAPLECUN_IP=192.168.1.55
      - MAPLECUN_PORT=2323
```

> Ohne Proxy (MapleCUN mit nur einem Modul): Modul 1 auf `IP-des-MapleCUN:2323`, die anderen auf „Aus“.

## Installation

### HACS (empfohlen)

1. HACS → ⋮ → **Benutzerdefinierte Repositories** → `https://github.com/rieders/ha-maplecun`, Typ **Integration**
2. „MapleCUN“ herunterladen, Home Assistant neu starten

### Manuell

Ordner `custom_components/maplecun` nach `/config/custom_components/` kopieren, Home Assistant neu starten.

## Einrichtung

**Einstellungen → Geräte & Dienste → Integration hinzufügen → MapleCUN**

* **Host** – IP des Rechners mit dem Split-Proxy
* **Modul 1–4** – Port und Aufgabe (Wireless M-Bus C/T/S, 433 MHz, Nur überwachen, Aus)
* **Empfangsmodus setzen** – schickt beim Verbinden `X21` bzw. `brc`/`brt`/`brs`
* **Intertechno-Codes** – optional, z. B. `FFFF00FFFF, FFFF0F0FFF`

### Geräte finden und übernehmen

Der MapleCUN hört alles in Funkreichweite – Zähler, Fernbedienungen und Energiemesser der Nachbarn eingeschlossen. Deshalb gilt:

1. Neu empfangene Geräte landen in einer Liste **„Gefundene Geräte“**; eine Benachrichtigung weist darauf hin.
2. **Konfigurieren → Gefundene Geräte übernehmen**: gewünschte Geräte anhaken, optional **„Alle nicht angehakten künftig ignorieren“**.
   Zähler sind nach Signalstärke sortiert – die eigenen sind meist die stärksten. Über Repeater empfangene Zähler sind gekennzeichnet.
3. Unter **Konfigurieren → Automatisch übernehmen** lässt sich je Gerätetyp festlegen, was ohne Nachfrage eingerichtet wird
   (Vorgabe: Revolt ja, Intertechno nein, Zähler nein) und ignorierte Geräte wieder einblenden.

Übernommene Geräte lassen sich auf ihrer Geräteseite wieder löschen.

### Wireless-M-Bus-Zähler

Viele Zähler senden nur wenige Male am Tag – Geduld. Die Sensoren (Zählerstand, Stichtagswert, Stichtag, Signalstärke …) entstehen beim ersten Telegramm nach der Übernahme; Zählerstände eignen sich direkt für das **Energie-Dashboard**.
Name und AES-Schlüssel (verschlüsselte Zähler) unter **Konfigurieren → Zähler bearbeiten**. Die Zähler-ID steht auf dem Gerät bzw. in FHEM im Namen: `WMBUS_LSE_52143909_26_7` → `52143909`.

### Intertechno

Wird eine Intertechno-Fernbedienung oder ein Wandschalter gedrückt, erscheint der Code unter *Gefundene Geräte*. Übernommene Steckdosen lassen sich schalten, und ihr Zustand folgt auch der Fernbedienung.
Codes aus FHEM: `list TYPE=IT DEF` → z. B. `FFFF0F0FFF FF F0` = Code, Ein, Aus. Manuell hinzufügen und Ein-/Aus-Codes ändern unter **Konfigurieren**.

## Technisches

* a-culfw liefert Wireless-M-Bus-Telegramme als `b…` (Rahmen A, mit Block-CRCs) bzw. `bY…` (Rahmen B) inkl. LQI/RSSI; alle CRCs werden geprüft
* OMS-Kurz-/Langheader, ELL (CI 8C), eingepackte Repeater-Telegramme, AES-128-CBC (Modus 5), DIF/VIF-Dekodierung
* Revolt-Dekodierung nach dem Aufbau von FHEM `19_Revolt.pm`, inkl. Plausibilitätsprüfung
* Verbindung je Modul per TCP (asyncio), automatischer Reconnect
* Intertechno-Empfang (`i…`, Tristate) nach dem Aufbau von FHEM `10_IT.pm`
* Upgrade von 0.1/0.2: bestehende Einträge und Revolts werden automatisch übernommen

## Alternative: wmbusmeters

Wer lieber [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters) nutzt: a-culfw antwortet auf `brc` mit `OFF`, wodurch wmbusmeters abbricht. [`extras/wmbusmeters`](extras/wmbusmeters) enthält eine Brücke, die das löst.

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
