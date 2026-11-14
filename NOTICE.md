# Herkunft und Lizenzen

Dieses Repository steht ab Version 0.6.0 unter der **GNU General Public License,
Version 2** (GPL-2.0-only), siehe [LICENSE](LICENSE). Versionen bis einschließlich
0.5.0 wurden unter der MIT-Lizenz veröffentlicht; für bereits verteilte Kopien
dieser Versionen gilt diese weiter.

Grund für den Wechsel ist die Portierung von FHEM-Code, der unter GPL v2 steht.
FHEM gibt keine Option „oder später“ an (`fhem.pl`: „GNU General Public License V2“),
daher ist auch dieses Projekt auf Version 2 festgelegt.

## Aus FHEM portiert

| Datei | Herkunft |
|---|---|
| `custom_components/maplecun/wmbus.py` | Extended Link Layer, AFL, Sicherheitsmodus 5 und 7, Rahmenformat B, langer Header, DIF/VIF-Auswertung: portiert aus FHEM `FHEM/WMBus.pm` (Autor: kaihs, FHEM-Forum), SVN-Revision 30375 vom 2025-10-11. Abweichungen von der Vorlage sind im Code mit „Abweichung von WMBus.pm“ markiert. |
| `custom_components/maplecun/wmbus_tables.py` | VIF-Tabellen `%VIFInfo`, `%VIFInfo_FD`, `%VIFInfo_FB`, `%VIFInfo_other`, `%VIFInfo_ESY`, `%VIFInfo_ESY2`, `%VIFInfo_KAM`, `%VIFInfo_KAM2` aus derselben Datei, maschinell übertragen. |
| `reference/WMBus.pm` | Unveränderte Kopie der Vorlage (bis auf die `$Id$`-Zeile identisch mit FHEM-SVN trunk, Stand 2026-11-12). Quelle: <https://svn.fhem.de/fhem/trunk/fhem/FHEM/WMBus.pm> |
| `custom_components/maplecun/climate433.py` | Protokolle aus FHEM `FHEM/14_CUL_TCM97001.pm` |
| `custom_components/maplecun/intertechno.py` | Aufbau nach FHEM `FHEM/10_IT.pm` |
| `custom_components/maplecun/revolt.py` | Aufbau nach FHEM `FHEM/19_Revolt.pm` |
| `custom_components/maplecun/culcmd.py`, Teile von `hub.py` | Kommandos und Umrechnungen der Modul-Abfragen und -Einstellungen (`get ccconf/cmds/credit10ms/uptime/version/raw`, `set freq/bWidth/rAmpl/sens/patable/led/ITClock/raw/reopen`, `@ampllist`) aus FHEM `FHEM/00_CUL.pm` (Autor: rudolfkoenig), `$Id: 00_CUL.pm 24815 2021-08-01`. Geprüft gegen a-culfw (<https://github.com/heliflieger/a-culfw>, GPL-2.0): `clib/cc1100.c`, `clib/fncollection.c`, `clib/rf_receive.c`, `clib/rf_mbus.c`, `clib/rf_mode.c`, `clib/intertechno.c`, `clib/multi_CC.c`; aus a-culfw wurde kein Code übernommen. |

FHEM: <https://fhem.de>, Lizenz GPL v2.

## Testdaten

`tests/test_wmbus_port.py` und `tests/test_maplecun.py` enthalten Rohtelegramme und
erwartete Messwerte aus folgenden Quellen. Übernommen wurden nur diese Daten, kein Code.

* **wmbusmeters** (<https://github.com/wmbusmeters/wmbusmeters>, GPL-3.0),
  Commit `f6ab1c28dab6f953198a4bfcbcfc353378951847`: `drivers/src/*.xmq`,
  `simulations/serial_aes.msg`, `tests/test_frame_b_trim.sh`, `src/testinternals.cc`
* **OMS Specification Vol. 2, Annex N**: Beispiel zum Sicherheitsmodus 7 (Kenc);
  der zugehörige Chiffretext stammt aus einem Kommentar in `WMBus.pm`
* **RFC 4493** (AES-CMAC): Prüfvektoren

`tests/test_wmbus.py` enthält zwei Telegramme, die mit dem eigenen MapleCUN
aufgezeichnet wurden, und eine direkt am Split-Proxy mitgeschnittene Zeile (T-Modus,
a-culfw 1.26.08) eines fremden Zählers – anonymisiert: ID ersetzt, Block-CRCs neu berechnet.
Dazu eine unveränderte Zeile des eigenen Warmwasserzählers 52144701 (S-Modus, über den
Netzwerkknoten).

`tests/culfw_sim.py` bildet das Verhalten von a-culfw nach (Antworten auf `V`, `?`, `br*`,
`X`, `C`, `t`); Grundlage ist der Quelltext von a-culfw
(<https://github.com/heliflieger/a-culfw>, GPL-2.0, Stand des Builds 323 vom 2019-08-03),
übernommen wurde kein Code.
