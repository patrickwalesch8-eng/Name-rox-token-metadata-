#!/usr/bin/env python3
"""Aktien Data Farm - command line.

  python farm.py fetch            5m-Daten von Yahoo laden (Historie waechst mit jedem Lauf)
  python farm.py run              Erkennung + Engine laufen lassen, Dashboard bauen
  python farm.py serve            Dashboard im Browser oeffnen
  python farm.py all              fetch + run + serve
  python farm.py demo             synthetische Demo-Daten erzeugen und Dashboard bauen (offline)
"""
import argparse
import http.server
import json
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from farmlib import data  # noqa: E402
from farmlib.build import build  # noqa: E402


def load_cfg():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def cmd_fetch(cfg, symbols):
    ok = 0
    for sym in symbols or cfg["symbols"]:
        try:
            df = data.fetch_yahoo(sym)
            total = data.save_bars(ROOT, sym, df)
            print(f"  {sym}: {len(df)} neue Bars geladen, {total} gespeichert")
            ok += 1
        except Exception as exc:  # keep going with the other symbols
            print(f"  {sym}: FEHLER {exc}")
    if not ok:
        sys.exit("Keine Daten geladen. Internetverbindung pruefen oder spaeter erneut versuchen.")


def cmd_serve(port):
    site = ROOT / "site"
    if not (site / "data.js").exists():
        sys.exit("Noch kein Dashboard gebaut - zuerst 'python farm.py run' (oder 'demo').")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(site), **kw)

        def log_message(self, *a):
            pass

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"Dashboard laeuft auf {url}  (Beenden mit Strg+C)")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    ap = argparse.ArgumentParser(description="Aktien Data Farm")
    ap.add_argument("cmd", choices=["fetch", "run", "serve", "all", "demo"])
    ap.add_argument("--symbols", nargs="*", help="z.B. --symbols AAPL MSFT (Standard: config.json)")
    ap.add_argument("--demo", action="store_true", help="run mit Demo-Daten statt echten Daten")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    cfg = load_cfg()
    if args.symbols:
        cfg["symbols"] = [s.upper() for s in args.symbols]

    if args.cmd in ("fetch", "all"):
        print("Lade Daten ...")
        cmd_fetch(cfg, cfg["symbols"])
    if args.cmd == "demo":
        print("Erzeuge Demo-Daten (synthetisch, KEINE echten Kurse) ...")
        for sym in cfg["symbols"]:
            data.save_bars(ROOT, sym, data.make_demo(sym), demo=True)
    if args.cmd in ("run", "all", "demo"):
        print("Analysiere ...")
        build(ROOT, cfg, demo=args.demo or args.cmd == "demo")
    if args.cmd in ("serve", "all"):
        cmd_serve(args.port)


if __name__ == "__main__":
    main()
