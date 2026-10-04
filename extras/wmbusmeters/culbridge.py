#!/usr/bin/env python3
"""
culbridge.py - verbindet ein MapleCUN-Modul (via maplecun-splitproxy, TCP)
mit einer virtuellen seriellen Schnittstelle fuer wmbusmeters.

Besonderheit a-culfw: "brc"/"brs"/"brt" schaltet den Empfangsmodus UM
(ist er schon aktiv -> "OFF"). wmbusmeters erwartet aber immer "CMODE" usw.
Daher:
  * Moduswechsel-Befehle von wmbusmeters werden NICHT weitergereicht,
    sondern direkt mit der erwarteten Antwort quittiert.
  * Die Bridge sorgt selbst dafuer, dass das Modul im richtigen Modus ist:
    Antwortet das Modul mit "OFF" (z.B. weil FHEM ebenfalls "brc" gesendet
    hat), wird der Modus sofort wieder eingeschaltet.

Aufruf: culbridge.py <host> <port> <c|s|t> <link>
"""
import os, sys, socket, select, time, tty, termios

HOST, PORT, MODE, LINK = sys.argv[1], int(sys.argv[2]), sys.argv[3].lower(), sys.argv[4]
REPLY = {"c": "CMODE", "s": "SMODE", "t": "TMODE"}
WANT = REPLY[MODE]

def log(*a):
    print(time.strftime("%H:%M:%S"), f"[culbridge {MODE.upper()}:{PORT}]", *a, flush=True)

# --- virtuelle Schnittstelle anlegen (bleibt ueber Reconnects bestehen) ---
master, slave = os.openpty()
tty.setraw(slave)
attrs = termios.tcgetattr(slave)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(slave, termios.TCSANOW, attrs)
try:
    os.unlink(LINK)
except FileNotFoundError:
    pass
os.symlink(os.ttyname(slave), LINK)
os.chmod(os.ttyname(slave), 0o666)
log("Schnittstelle", LINK, "->", os.ttyname(slave))

def connect():
    while True:
        try:
            s = socket.create_connection((HOST, PORT), timeout=10)
            s.settimeout(None)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            log("verbunden mit", f"{HOST}:{PORT}")
            return s
        except OSError as e:
            log("Verbindung fehlgeschlagen:", e, "- neuer Versuch in 5 s")
            time.sleep(5)

def run():
    sock = connect()
    from_dev = b""
    from_app = b""
    last_fix = 0.0
    # Modus sicherstellen: senden; antwortet das Modul mit OFF, greift die Logik unten
    sock.sendall(f"br{MODE}\r\n".encode())
    while True:
        r, _, _ = select.select([sock, master], [], [], 60)
        if sock in r:
            data = sock.recv(4096)
            if not data:
                raise ConnectionError("Proxy hat Verbindung geschlossen")
            from_dev += data
            while b"\n" in from_dev:
                line, from_dev = from_dev.split(b"\n", 1)
                txt = line.strip().decode(errors="replace")
                if txt == "OFF":
                    if time.time() - last_fix > 2:
                        log("Modul meldet OFF -> schalte", WANT, "wieder ein")
                        sock.sendall(f"br{MODE}\r\n".encode())
                        last_fix = time.time()
                    continue          # OFF nicht an wmbusmeters geben
                if txt in ("CMODE", "SMODE", "TMODE"):
                    log("Modul meldet", txt)
                    continue          # Quittung bekommt wmbusmeters von uns
                os.write(master, line + b"\n")
        if master in r:
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            from_app += data
            while b"\n" in from_app:
                line, from_app = from_app.split(b"\n", 1)
                cmd = line.strip().decode(errors="replace")
                if cmd.startswith("br") and len(cmd) == 3:
                    log(f"wmbusmeters will '{cmd}' -> quittiere mit {WANT}")
                    os.write(master, (WANT + "\r\n").encode())
                    continue
                if cmd:
                    sock.sendall(line.rstrip(b"\r") + b"\r\n")

while True:
    try:
        run()
    except (OSError, ConnectionError) as e:
        log("Fehler:", e, "- verbinde neu in 5 s")
        time.sleep(5)
