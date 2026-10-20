# MT5-Autopilot: die Data Farm handelt selbst (Demokonto)

Der Autopilot handelt die SMC-Strategie (Sweep → MSS → FVG/OB) selbstständig auf einem
**MetaTrader-5-Demokonto**.
Das Sicherheitskonzept stammt von deinem Trend-Bot und wurde erweitert.

> Der Bot startet **nur auf Demokonten**. Lass ihn dort mehrere Wochen laufen und vergleiche
> die Ergebnisse mit dem Backtest, bevor du überhaupt über echtes Geld nachdenkst.

## Was du brauchst

1. **MetaTrader 5** am PC und ein **Demokonto bei einem Broker, der US-Aktien-CFDs anbietet**.
   Prüfe im MT5 unter *Ansicht → Marktübersicht → Symbole*, ob es dort zum Beispiel NVDA,
   AAPL oder TSLA gibt.
2. **Python** (wie beim Trend-Bot). Am besten Python 3.12, weil das MetaTrader5-Paket nicht
   immer sofort die neueste Python-Version unterstützt.
3. **Symbolnamen:** Übliche Broker-Namen wie `NVDA.US`, `#NVDA` oder `NVDAm` erkennt der Bot
   beim Start selbst und schreibt ins Fenster, welchen Namen er nimmt. Gibt es mehrere passende
   Namen, rät er nicht, sondern listet sie auf. Dann den richtigen in `mt5_config.py` unter
   `MARKETS` eintragen, zum Beispiel `"NVDA": "NVDA.US"`.
4. **Serverzeit-Regel prüfen:** MT5 liefert alle Kerzen in der Uhrzeit des Brokers. Die meisten
   Broker laufen auf „New York + 7 Stunden“, das ist in `mt5_config.py` als `SERVER_TZ = "NY+7"`
   voreingestellt. Der Bot prüft das an den Live-Kursen, sobald neue Kurse hereinkommen. Passt
   es nicht, steht im Fenster zum Beispiel „Broker-Serverzeit ist UTC+2.0 h …“, und er
   platziert keine Orders. Prüfe dann **zuerst die Windows-Uhr**: Die Meldung nennt die
   New-York-Zeit, die der PC errechnet. Stimmt die nicht (z.B. mit time.is vergleichen), ist die
   Uhr oder die Zeitzone in Windows falsch eingestellt. Stimmt sie, `SERVER_TZ` anpassen:
   - `"Europe/Athens"` für GMT+2/+3 mit europäischer Umstellung
   - `"UTC+2"` für eine feste Zeit ohne Umstellung

## Starten

1. MT5 öffnen und mit dem **Demokonto** einloggen.
2. Oben den Knopf **„Algo Trading“** einschalten, sodass er grün ist.
   Unter **Extras → Optionen → Expert Advisors** muss außerdem gelten:
   - Haken bei **„Algorithmischen Handel erlauben“** gesetzt
   - **kein** Haken bei der Option, die den Algo-Handel **über die externe Python-API deaktiviert**.
     Ist sie an, darf der Bot Kurse lesen, aber keine Orders senden (Retcode 10027).
3. Im Ordner `trading-farm/mt5` die Datei **`start_mt5_bot.bat` doppelklicken**.
   Beim ersten Mal wird alles installiert.
4. Nach dem Verbinden muss im Fenster stehen: **„Handel über die MT5-Python-API ist erlaubt.“**
   Steht dort **„KEIN HANDEL: …“**, nennt die Zeile den Grund und was zu tun ist. Eine Änderung
   im MT5 bemerkt der Bot von selbst, ein Neustart ist nicht nötig.

Gestoppt wird mit **Strg + C**. Dabei werden wartende Limit-Orders gelöscht.
Offene Positionen behalten ihren Stop-Loss und Take-Profit beim Broker, werden ohne laufenden
Bot aber **nicht zum Schluss glattgestellt**.

**Wichtig:** Der Autopilot läuft **auf deinem PC**.
Der PC muss während der US-Handelszeit an sein, also 15:30–22:00 Uhr deutscher Zeit.
Ausnahme: Wenn Europa und die USA die Uhren an verschiedenen Tagen umstellen, ist es eine
Stunde früher (14:30–21:00 Uhr). 2026 betrifft das den 25.10. bis 31.10.; im März meist
zwei bis drei Wochen.

## Was der Bot macht

Bei jeder neuen 5-Minuten-Kerze in der regulären US-Handelszeit:

1. Er holt die Kerzen von MT5 und rechnet die Broker-Serverzeit automatisch in New-York-Zeit um.
2. Er lässt die Mustererkennung und die Engine laufen. Die Einstellungen kommen aus `../config.json`.
3. Hat die Engine **genau auf dieser Kerze** ein Setup scharf gemacht, setzt er eine
   **Limit-Order mit Stop-Loss und Take-Profit** beim Broker.
4. Verwirft die Engine das Setup (kein Retest, Kurs weggelaufen, Zeit abgelaufen),
   löscht er die Limit-Order.
5. Mit der Kerze, die um **15:55 New York** endet, stellt er alles glatt. An **halben
   Handelstagen** (Schluss um 13:00, z.B. 27.11. und 24.12.) passiert das schon um 12:55.
   Danach hält er nichts mehr, auch nicht über Nacht. Die halben Tage stehen in `../config.json`
   unter `early_close` und müssen jedes Jahr ergänzt werden
   (NYSE-Feiertagskalender: nyse.com/markets/hours-calendars).

## Sicherheit

| Schutz | Was passiert |
|---|---|
| Nur Demokonto | Auf Echtgeldkonten bricht der Bot ab (`ALLOW_REAL_ACCOUNT = False`). Wechselt jemand im laufenden Terminal das Konto, handelt er nicht mehr, bis das Startkonto zurück ist. |
| Order-Verfall | Jede Limit-Order verfällt **beim Broker** von selbst, spätestens um 15:55. Das gilt auch, wenn der Bot abstürzt oder der PC ausgeht. Erlaubt der Broker keine Ablaufzeit, platziert der Bot keine Orders. |
| SL/TP-Wächter | Jede Position braucht SL und TP beim Broker. Fehlt etwas, rüstet er es nach, sonst schließt er die Position. Das wird in jeder Runde geprüft. |
| Lotgröße | Der Broker rechnet selbst (`order_calc_profit`). Die Größe wird immer abgerundet, und reicht die Margin nicht, wird sie verkleinert. |
| Keine Doppel-Orders | Höchstens eine Position oder Order pro Aktie und `MAX_OPEN_POSITIONS` insgesamt. Nach einer Teilausführung wird nichts erneut gesendet. |
| Tagesverlust-Grenze | Bei −3 % vom Kapital zu Tagesbeginn kommen an diesem Tag keine neuen Orders mehr (`DAILY_LOSS_LIMIT_PERCENT`, 0 = aus). Diese Grenze gibt es im Backtest nicht. |
| Zeitumstellung | Die Serverzeit wird für jede Kerze einzeln umgerechnet, also auch über Sommer-/Winterzeit hinweg richtig. Passt die Regel nicht zu den Live-Kursen, kommen keine Orders. |
| PC-Uhr | Geht die Windows-Uhr mehr als 1 Minute falsch, kommen keine neuen Orders. Glattgestellt wird dann nach der Uhr des Brokers, also trotzdem rechtzeitig. |
| Fehler | Ein einzelner Fehler (z.B. Journal in Excel geöffnet) stoppt den Bot nicht. Journal-Zeilen werden später nachgetragen. |
| Nur einmal starten | Ein zweites Bot-Fenster im selben Ordner bricht sofort ab. |
| Spread-Filter | Kein Trade, wenn der Spread mehr als 20 % des Stop-Abstands ausmacht. |
| Handel gesperrt | Keine neuen Orders, solange „Algo Trading“ aus ist, die Python-API im MT5 gesperrt ist, mit dem Investor-Passwort (nur Lesen) eingeloggt ist oder der Broker keinen Automatik-Handel erlaubt. Der Grund steht im Fenster. |
| Verbindung weg | Der Bot verbindet sich automatisch neu. |
| Neustart | Der Zustand liegt in `bot_smc_state.json`. Alte Signale werden nicht nachgehandelt. Was der Broker ausgeführt hat, während der Bot aus war (Order gefüllt, SL/TP getroffen), wird im Journal nachgetragen. |

Mit deinem Trend-Bot kann er **gleichzeitig auf demselben Konto** laufen. Er hat eine eigene
Kennung (`MAGIC_NUMBER = 200002`) und fasst die Trades des anderen Bots nicht an.

## Ergebnisse ansehen

- **`trades_smc.csv`** enthält jede Order, jeden Einstieg und Ausstieg (mit Gewinn und R),
  jede Stornierung und jedes übersprungene Setup samt Grund. Die Datei lässt sich in Excel
  öffnen (Trennzeichen Semikolon).
- **`bot_smc.log`** ist das komplette Protokoll. Alle 15 Minuten steht dort pro Aktie der Status.

## Probleme

| Meldung | Lösung |
|---|---|
| `MetaTrader5-Paket fehlt` / Installation schlägt fehl | Python 3.12 installieren und `start_mt5_bot.bat` erneut starten |
| `Symbol 'NVDA' beim Broker nicht gefunden` | Den vorgeschlagenen Namen in `mt5_config.py` → `MARKETS` eintragen. Bietet dein Broker die Aktie gar nicht an, lässt der Bot sie einfach weg. |
| `MT5-Initialisierung fehlgeschlagen` | MT5 starten und einloggen. Bot und MT5 beide normal starten, nicht eins davon „Als Administrator“. Sind mehrere MT5 installiert, in `mt5_config.py` bei `MT5_PATH` den Pfad zur richtigen `terminal64.exe` eintragen. |
| `KEIN HANDEL: …` | Die Zeile sagt, was zu tun ist: „Algo Trading“ einschalten, den Haken für die Python-API-Sperre entfernen (Schritt 2 unter „Starten“) oder mit dem normalen statt dem Investor-Passwort einloggen. |
| `Order abgelehnt (Retcode …)` | Hinter der Nummer steht der Grund, z.B. „Markt geschlossen“. Steht keiner dabei, die Nummer im MT5-Journal nachsehen oder mir schicken. |
| `Die PC-Uhr geht … s vor/nach` | Windows: Einstellungen → Zeit und Sprache → Datum und Uhrzeit → „Jetzt synchronisieren“. Der Bot merkt es selbst, wenn die Uhr wieder stimmt. |
| `Broker erlaubt keine Ablaufzeit für Limit-Orders` | Bei diesem Broker verfallen Limit-Orders nicht von selbst. Der Bot handelt dort nicht, weil eine Order sonst nach einem Absturz noch nach Börsenschluss gefüllt werden könnte. Ein anderer Broker bzw. anderes Demokonto ist nötig. |
| Es kommen keine Orders | Das ist normal, die Strategie ist sehr wählerisch: Im Backtest mit echten Kursen gab es bei 5 Aktien **20 Trades in 60 Tagen**, also nur alle paar Tage einen. Orders kommen nur von 15:45 bis 21:30 Uhr deutscher Zeit. Außerhalb steht im Status „warte auf Daten / Markt geschlossen“. Die Gründe für ausgelassene Setups stehen als `SKIP` im Journal. |
