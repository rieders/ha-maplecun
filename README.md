# MapleCUN für Home Assistant

Custom Integration, die alle Funkmodule eines **[MapleCUN](https://wiki.fhem.de/wiki/MapleCUN)** (a-culfw, bis zu 4 CC1101-Module) direkt in Home Assistant bringt – ohne FHEM, ohne wmbusmeters.

| Aufgabe eines Moduls | Geräte in Home Assistant |
|---|---|
| **Wireless M-Bus C / T / S** | Wasser-, Wärme-, Gaszähler, Heizkostenverteiler (OMS): Zählerstand, Stichtags- und Monatsendwert, Signalstärke, zuletzt empfangen … – auch über Netzwerkknoten/Repeater empfangene und AES-verschlüsselte Zähler (Modus 5) |
| **433 MHz** | Revolt NC-5462 Energiemesser, Intertechno-Steckdosen (Tristate), Thermometer (Mebus, GT-WT-02, KW9010, NX7674) |
| **Nur überwachen** | z. B. ein HomeMatic-/MAX!-Modul, das weiter von FHEM bedient wird – nur Verbindungsstatus |

Jedes Modul bekommt Diagnose-Sensoren für Verbindung, **letzten Empfang** und **Telegramme heute**; dazu **„Empfangene Zähler“** mit den eingerichteten Wireless-M-Bus-Zählern.

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

1. Neu empfangene 433-MHz-Geräte landen in einer Liste **„Gefundene Geräte“**; eine Benachrichtigung weist darauf hin.
2. **Konfigurieren → Gefundene Geräte übernehmen**: gewünschte Geräte anhaken, optional **„Alle nicht angehakten künftig ignorieren“**.
3. Unter **Konfigurieren → Einstellungen** lässt sich je Gerätetyp festlegen, was ohne Nachfrage eingerichtet wird
   (Vorgabe: Revolt ja, Intertechno nein) und ignorierte Geräte wieder einblenden.

Wireless-M-Bus-Zähler werden **nicht** gesammelt: Telegramme von Zählern, deren ID nicht eingetragen ist, werden verworfen und nirgends gespeichert.

Übernommene Geräte lassen sich auf ihrer Geräteseite wieder löschen.

### Wireless-M-Bus-Zähler

Zähler-IDs unter **Konfigurieren → Wireless-M-Bus-Zähler hinzufügen** eintragen. Die ID steht auf dem Gerät bzw. in FHEM im Namen: `WMBUS_LSE_52143909_26_7` → `52143909`.
Zum Suchen der eigenen IDs lässt sich unter **Konfigurieren → Einstellungen** vorübergehend **„Unbekannte Zähler ins Log schreiben“** einschalten (nur ID, Typ, RSSI; Vorgabe aus).
Welches Modul Wireless M-Bus empfängt und in welchem Modus (S, T, C), steht unter **Konfigurieren → Funkmodule**; der Modus wird beim Verbinden per `brs`/`brt`/`brc` gesetzt.

Viele Zähler senden nur ein- bis wenige Male am Tag – Geduld. Die Sensoren entstehen beim ersten Telegramm nach dem Eintragen und behalten ihren Wert über Neustarts:

* **Wasser** (warm/kalt): Zählerstand in m³ (`total_increasing`, direkt für das **Energie-Dashboard**)
* **Heizkostenverteiler**: Verbrauchseinheiten (dimensionslos, `total_increasing`, am Stichtag zurückgesetzt)
* Diagnose: Stichtagswert und Stichtag, Monatsendwert und Monatsende, Signalstärke, zuletzt empfangen

Name und AES-Schlüssel (verschlüsselte Zähler) unter **Konfigurieren → Zähler bearbeiten**; verschlüsselte Telegramme ohne Schlüssel werden nur geloggt.

### Thermometer (433 MHz)

Unterstützt werden Protokolle mit Prüfsumme (aus FHEM `14_CUL_TCM97001.pm` portiert): **Mebus** (u. a. Hama), **GT-WT-02 / Type1** (Temperatur + Feuchte), **KW9010** (Tchibo/TFA, Temperatur + Feuchte) und **NX7674** (Rosenstein & Söhne Kühlschrank-Thermometer).
Gefundene Thermometer erscheinen unter *Gefundene Geräte* mit aktueller Temperatur – so lässt sich das eigene leicht erkennen. Viele Thermometer bekommen nach einem Batteriewechsel eine neue Kennung und müssen dann neu übernommen werden.

### Intertechno

Wird eine Intertechno-Fernbedienung oder ein Wandschalter gedrückt, erscheint der Code unter *Gefundene Geräte*. Übernommene Steckdosen lassen sich schalten, und ihr Zustand folgt auch der Fernbedienung.
Codes aus FHEM: `list TYPE=IT DEF` → z. B. `FFFF0F0FFF FF F0` = Code, Ein, Aus. Manuell hinzufügen und Ein-/Aus-Codes ändern unter **Konfigurieren**.

## Technisches

* a-culfw liefert Wireless-M-Bus-Telegramme als `b…` (Rahmen A, mit Block-CRCs) bzw. `bY…` (Rahmen B, auch über zwei CRC-Blöcke) plus ein angehängtes Statusbyte; alle CRCs werden geprüft (CRC-16/EN-13757), Telegramme mit CRC-Fehler verworfen
* Der Wireless-M-Bus-Decoder ist eine Portierung von FHEM `WMBus.pm` (siehe [NOTICE.md](NOTICE.md)): OMS-Kurz-/Langheader (CI 7A/72/78), Extended Link Layer (CI 8C/8D/8E/8F, AES-128-CTR), AFL (CI 90), AES-128-CBC Sicherheitsmodus 5 und 7 (Schlüsselableitung per AES-CMAC, MAC-Prüfung), DIF/VIF-Dekodierung mit den vollständigen VIF-Tabellen aus FHEM (inkl. FD/FB, kombinierbarer VIFEs, Klartext-VIF, Easymeter/Kamstrup)
* Wo `WMBus.pm` von EN 13757 abweicht (Speichernummern aus DIFE, Länge der ELL-Payload-CRC, Reihenfolge von Zeichenketten, Korrekturfaktor-VIFEs), folgt der Port der Norm – belegt mit echten Telegrammen; die Stellen sind im Code markiert
* Nicht unterstützt: fragmentierte AFL-Telegramme, SML-Nutzdaten (CI 7E/7F), Kamstrup-Compact-Frames (CI 79), herstellerspezifische CI-Felder
* Über Netzwerkknoten weitergeleitete Telegramme (C=C4, CI=A0, Hersteller `LSE` + Bit 15): das innere Zählertelegramm ab Offset 18 wird bis zum Ende des äußeren Telegramms ausgewertet (das innere L-Feld ist unzuverlässig). An diesem Rahmen scheitern FHEM (`Unsupported CI Field a0`) und vermutlich auch wmbusmeters
* Der Decoder (`wmbus.py`) hat keine Home-Assistant-Abhängigkeiten: `tests/test_wmbus.py` und `tests/test_wmbus_port.py` laufen mit reinem `pytest` (+ `cryptography` für AES)
* a-culfw schaltet bei `brc`/`brt`/`brs` um: meldet ein Wireless-M-Bus-Modul `OFF`, wird der Modus einmal erneut gesetzt
* Revolt-Dekodierung nach dem Aufbau von FHEM `19_Revolt.pm`, inkl. Plausibilitätsprüfung
* Verbindung je Modul per TCP (asyncio), automatischer Reconnect
* Intertechno-Empfang (`i…`, Tristate) nach dem Aufbau von FHEM `10_IT.pm`
* Upgrade von 0.1/0.2: bestehende Einträge und Revolts werden automatisch übernommen

## Alternative: wmbusmeters

Die eingebaute Dekodierung ist der Standard. Wer lieber [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters) nutzt: a-culfw antwortet auf `brc` mit `OFF`, wodurch wmbusmeters abbricht. [`extras/wmbusmeters`](extras/wmbusmeters) enthält eine Brücke, die das löst.

## Tests

```bash
pip install -r requirements_test.txt
pytest tests
```

Nur den Decoder (ohne Home Assistant): `pip install pytest cryptography && pytest tests/test_wmbus.py tests/test_wmbus_port.py`

## Danksagung

* [a-culfw](https://github.com/heliflieger/a-culfw) und die FHEM-Community, insbesondere kaihs für `WMBus.pm`
* [boriswde/maplecun-splitproxy](https://hub.docker.com/r/boriswde/maplecun-splitproxy)
* [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters) – Testtelegramme und erwartete Werte

## Lizenz

GPL-2.0-only (bis Version 0.5.0: MIT). Herkunft der portierten Teile und der Testdaten: [NOTICE.md](NOTICE.md).
