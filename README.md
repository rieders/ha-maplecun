# MapleCUN für Home Assistant

Custom Integration, die alle Funkmodule eines **[MapleCUN](https://wiki.fhem.de/wiki/MapleCUN)** (a-culfw, bis zu 4 CC1101-Module) direkt in Home Assistant bringt – ohne FHEM, ohne wmbusmeters.

| Aufgabe eines Moduls | Geräte in Home Assistant |
|---|---|
| **Wireless M-Bus C / T / S** | Wasser-, Wärme-, Gaszähler, Heizkostenverteiler (OMS): Zählerstand, Stichtags- und Monatsendwert, Signalstärke, zuletzt empfangen … – auch über Netzwerkknoten/Repeater empfangene und AES-verschlüsselte Zähler (Modus 5) |
| **433 MHz** | Revolt NC-5462 Energiemesser, Intertechno-Steckdosen (Tristate), Thermometer (Mebus, GT-WT-02, KW9010, NX7674) |
| **Nur überwachen** | z. B. ein HomeMatic-/MAX!-Modul, das weiter von FHEM bedient wird – nur Verbindungsstatus |

Jedes Modul bekommt Diagnose-Sensoren für Verbindung, **letzten Empfang** und **Telegramme heute** sowie einen (standardmäßig deaktivierten) Sensor **„Rohdaten Modul N“**; dazu die Zählerübersicht **„Empfangene Zähler“** (siehe unten).

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

Wireless-M-Bus-Zähler werden **nicht** gesammelt: Von Zählern, deren ID nicht eingetragen ist, werden keine Messwerte dekodiert und nichts gespeichert. Nur ihre Kopfdaten (ID, Hersteller, Typ, Signalstärke) stehen bis zum nächsten Neustart in der [Zählerübersicht](#zählerübersicht) und als Vorschläge unter **Zähler hinzufügen**.

Übernommene Geräte lassen sich auf ihrer Geräteseite wieder löschen.

### Wireless-M-Bus-Zähler

Zähler-IDs unter **Konfigurieren → Wireless-M-Bus-Zähler hinzufügen** eintragen. Die ID steht auf dem Gerät bzw. in FHEM im Namen: `WMBUS_LSE_52143909_26_7` → `52143909`.
Beim Hinzufügen schlägt eine Auswahlliste die seit dem Start gehörten, noch nicht angelegten IDs vor (ID, Typ, RSSI, stärkstes Signal zuerst – der eigene Zähler ist meist der nächste); freie Eingabe bleibt möglich.
Alternativ lässt sich unter **Konfigurieren → Einstellungen** vorübergehend **„Unbekannte Zähler ins Log schreiben“** einschalten (nur ID, Typ, RSSI; Vorgabe aus).
Welches Modul Wireless M-Bus empfängt und in welchem Modus (S, T, C), steht unter **Konfigurieren → Funkmodule**; der Modus wird beim Verbinden per `brs`/`brt`/`brc` gesetzt.

Viele Zähler senden nur ein- bis wenige Male am Tag – Geduld. Die Sensoren entstehen beim ersten Telegramm nach dem Eintragen und behalten ihren Wert über Neustarts:

* **Wasser** (warm/kalt): Zählerstand in m³ (`total_increasing`, direkt für das **Energie-Dashboard**)
* **Heizkostenverteiler**: Verbrauchseinheiten (dimensionslos, `total_increasing`, am Stichtag zurückgesetzt)
* Diagnose: Stichtagswert und Stichtag, Monatsendwert und Monatsende, Signalstärke, zuletzt empfangen (Zeitstempel, bleibt über Neustarts erhalten)

Name und AES-Schlüssel (verschlüsselte Zähler) unter **Konfigurieren → Zähler bearbeiten**; verschlüsselte Telegramme ohne Schlüssel werden nur geloggt.

### Zählerübersicht

Der Diagnose-Sensor **„Empfangene Zähler“** (am Gerät „MapleCUN …“) zeigt als Zustand die Anzahl der seit dem Start empfangenen unterschiedlichen Zähler. Zwei Attribute enthalten die Listen:

* `eigene` – alle angelegten Zähler, auch noch nie empfangene (`letzter_empfang` leer)
* `weitere` – nicht angelegte Zähler, stärkstes Signal zuerst, höchstens 200

Jeder Eintrag hat `id`, `name`, `hersteller`, `typ`, `rssi`, `modul`, `netzwerkknoten` (true/false), `letzter_empfang` (ISO 8601) und `telegramme_heute`.
Für nicht angelegte Zähler werden nur die Kopfdaten ausgewertet (keine Entschlüsselung, keine Messwerte). Die Liste `weitere` lebt nur im Arbeitsspeicher, wird weder in die Konfiguration noch auf die Platte geschrieben und beginnt nach einem Neustart leer. Beide Attribute sind vom Recorder ausgenommen und landen nicht in der Datenbank.

**Dashboard-Karte** (Markdown-Karte; die Entity-ID in der zweiten Zeile ggf. anpassen – zu finden unter Einstellungen → Entitäten → „Empfangene Zähler“, Vorgabe `sensor.maplecun_<IP>_empfangene_zahler`):

```yaml
type: markdown
title: Wireless-M-Bus-Zähler
content: |-
  {%- set sensor = 'sensor.maplecun_192_168_1_228_empfangene_zahler' -%}
  {%- set schwelle_h = 48 -%}{# eigene Zähler ohne Empfang seit so vielen Stunden hervorheben #}
  {%- set max_weitere = 30 -%}
  {%- macro absolut(ts) -%}
  {{ as_timestamp(ts) | timestamp_custom('%d.%m.%Y %H:%M') if ts else 'nie' }}
  {%- endmacro -%}
  {%- macro relativ(ts) -%}
  {%- if ts -%}
  {%- set m = ((now() - as_datetime(ts)).total_seconds() / 60) | int -%}
  {%- set h = (m / 60) | int -%}
  {%- set d = (m / 1440) | int -%}
  {%- if m < 1 -%}gerade eben
  {%- elif m < 60 -%}vor {{ m }} {{ 'Minute' if m == 1 else 'Minuten' }}
  {%- elif h < 48 -%}vor {{ h }} {{ 'Stunde' if h == 1 else 'Stunden' }}
  {%- else -%}vor {{ d }} Tagen{%- endif -%}
  {%- else -%}–{%- endif -%}
  {%- endmacro -%}
  {%- macro dbm(v) -%}{{ v ~ ' dBm' if v is not none else '–' }}{%- endmacro %}
  ### Eigene Zähler

  | | ID | Name | Typ | RSSI | Modul | Knoten | Letzter Empfang | | Heute |
  |---|---|---|---|---|---|---|---|---|---|
  {%- for z in state_attr(sensor, 'eigene') or [] %}
  {%- set ts = z.letzter_empfang %}
  {%- set stumm = not ts or (now() - as_datetime(ts)).total_seconds() > schwelle_h * 3600 %}
  {%- set b = '**' if stumm else '' %}
  | {{ '⚠️' if stumm else '✅' }} | {{ b }}{{ z.id }}{{ b }} | {{ b }}{{ z.name }}{{ b }} | {{ z.typ or '–' }} | {{ dbm(z.rssi) }} | {{ z.modul or '–' }} | {{ 'ja' if z.netzwerkknoten else 'nein' }} | {{ b }}{{ absolut(ts) }}{{ b }} | {{ b }}{{ relativ(ts) }}{{ b }} | {{ z.telegramme_heute }} |
  {%- endfor %}

  ⚠️ = seit mehr als {{ schwelle_h }} Stunden (oder noch nie) empfangen

  ### Weitere Zähler in Reichweite ({{ (state_attr(sensor, 'weitere') or []) | count }})

  | ID | Hersteller | Typ | RSSI | Modul | Knoten | Letzter Empfang | | Heute |
  |---|---|---|---|---|---|---|---|---|
  {%- for z in (state_attr(sensor, 'weitere') or [])[:max_weitere] %}
  | {{ z.id }} | {{ z.hersteller }} | {{ z.typ }} | {{ dbm(z.rssi) }} | {{ z.modul }} | {{ 'ja' if z.netzwerkknoten else 'nein' }} | {{ absolut(z.letzter_empfang) }} | {{ relativ(z.letzter_empfang) }} | {{ z.telegramme_heute }} |
  {%- endfor %}
```

**Benachrichtigung, wenn ein angelegter Zähler länger als 48 Stunden nichts gesendet hat** (Einstellungen → Automatisierungen → neu → ⋮ → In YAML bearbeiten; Entity-ID und Benachrichtigungsdienst anpassen):

```yaml
alias: Wireless-M-Bus-Zähler meldet sich nicht
description: Angelegter Zähler seit mehr als 48 Stunden ohne Telegramm
mode: single
triggers:
  - trigger: template
    value_template: >-
      {%- set ns = namespace(n=0) -%}
      {%- for z in state_attr('sensor.maplecun_192_168_1_228_empfangene_zahler', 'eigene') or [] -%}
        {%- if z.letzter_empfang and now() - as_datetime(z.letzter_empfang) > timedelta(hours=48) -%}
          {%- set ns.n = ns.n + 1 -%}
        {%- endif -%}
      {%- endfor -%}
      {{ ns.n > 0 }}
actions:
  - action: notify.notify
    data:
      title: Zähler ohne Empfang
      message: |-
        {%- for z in state_attr('sensor.maplecun_192_168_1_228_empfangene_zahler', 'eigene') or [] -%}
          {%- if z.letzter_empfang and now() - as_datetime(z.letzter_empfang) > timedelta(hours=48) %}
        {{ z.name }} ({{ z.id }}): zuletzt {{ as_timestamp(z.letzter_empfang) | timestamp_custom('%d.%m.%Y %H:%M') }}
          {%- endif -%}
        {%- endfor %}
```

Der Template-Trigger wird jede Minute neu ausgewertet und löst aus, sobald der erste Zähler die Schwelle überschreitet (nicht erneut für jeden weiteren, solange einer stumm ist). Noch nie empfangene Zähler werden ausgelassen.
Für einen einzelnen Zähler geht es auch mit seinem Diagnose-Sensor „Zuletzt empfangen“:
`{{ now() - states('sensor.warmwasser_bad_zuletzt_empfangen') | as_datetime > timedelta(hours=48) }}`

### Rohdaten

**„Rohdaten Modul N“** zeigt die letzte Rohzeile eines Moduls (433 MHz: nicht ausgewertete Zeilen; Wireless M-Bus: jede Telegrammzeile). Der Zustand ist auf 255 Zeichen gekürzt (Grenze von Home Assistant), die vollständige Zeile steht im Attribut `line` (nicht aufgezeichnet).
Die Sensoren sind standardmäßig deaktiviert. Bei Wireless-M-Bus-Modulen laufen dort **auch Telegramme fremder Zähler** durch und landen – gekürzt – im Verlauf; nur zur Fehlersuche einschalten.

### Thermometer (433 MHz)

Unterstützt werden Protokolle mit Prüfsumme (aus FHEM `14_CUL_TCM97001.pm` portiert): **Mebus** (u. a. Hama), **GT-WT-02 / Type1** (Temperatur + Feuchte), **KW9010** (Tchibo/TFA, Temperatur + Feuchte) und **NX7674** (Rosenstein & Söhne Kühlschrank-Thermometer).
Gefundene Thermometer erscheinen unter *Gefundene Geräte* mit aktueller Temperatur – so lässt sich das eigene leicht erkennen. Viele Thermometer bekommen nach einem Batteriewechsel eine neue Kennung und müssen dann neu übernommen werden.

### Intertechno

Wird eine Intertechno-Fernbedienung oder ein Wandschalter gedrückt, erscheint der Code unter *Gefundene Geräte*. Übernommene Steckdosen lassen sich schalten, und ihr Zustand folgt auch der Fernbedienung.
Codes aus FHEM: `list TYPE=IT DEF` → z. B. `FFFF0F0FFF FF F0` = Code, Ein, Aus. Manuell hinzufügen und Ein-/Aus-Codes ändern unter **Konfigurieren**.

## Technisches

* a-culfw liefert Wireless-M-Bus-Telegramme als `b…` (Rahmen A, mit Block-CRCs) bzw. `bY…` (Rahmen B, auch über zwei CRC-Blöcke) plus LQI- und RSSI-Byte (mit `X21`; im FHEM-Log fehlt das RSSI-Byte); alle CRCs werden geprüft (CRC-16/EN-13757), Telegramme mit CRC-Fehler verworfen
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
