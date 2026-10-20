# Aktien Data Farm auf TradingView

Das Script `aktien_data_farm.pine` bringt die Strategie der Data Farm direkt in TradingView.
Es zeichnet alle Muster in den Chart: Liquiditäts-Pools, Sweeps, BOS/MSS, Fair Value Gaps,
Order Blocks und HH/HL/LH/LL. Im Strategietester bekommst du einen Backtest.
Außerdem verschickt es Alarme, die auf den TradingView-Servern laufen. Du bekommst sie
also rund um die Uhr aufs Handy, **auch wenn PC und Browser aus sind**.

> Das Script **handelt nicht selbst** mit echtem Geld. Es meldet dir Setups, Einstiege und
> Ausstiege, und ob du handelst, entscheidest du. Backtest-Ergebnisse sind keine Garantie.

## 1. Script einfügen (einmalig, am PC)

1. tradingview.com öffnen und einen Chart öffnen, z. B. **NVDA**.
2. Unten bzw. rechts den **Pine Editor** öffnen.
3. **Neu → Leere Strategie** wählen und den vorhandenen Text komplett löschen.
4. Den gesamten Inhalt von `aktien_data_farm.pine` hineinkopieren.
   - Am Handy auf GitHub: Datei öffnen, dann „Raw“ wählen, alles markieren und kopieren.
   - Am PC: Datei öffnen, dann das Kopier-Symbol oben rechts im Dateifenster klicken.
5. **Speichern** (Name z. B. „Aktien Data Farm“) und auf **„Zum Chart hinzufügen“** klicken.

Erscheint eine rote Fehlermeldung, kopiere sie mir einfach. Das Script wurde gründlich
geprüft, aber nicht im echten TradingView kompiliert.

## 2. Chart richtig einstellen

- **Zeiteinheit: 5 Minuten.**
- **Nur reguläre Handelszeit:** In den Chart-Einstellungen unter **Symbol → Session**
  die Option **„Reguläre Handelszeiten“** wählen. Vorbörse und nachbörslicher Handel bleiben aus.
- Im Tab **Strategietester** unten siehst du Trefferquote, Profit-Faktor, Drawdown
  und die Liste aller Trades.
- Die **Farm-Tabelle** oben rechts im Chart zeigt den Status („beobachte“,
  „Sweep – warte auf MSS“, „Limit aktiv“, „im Trade“), den 1h-Trend, die Trades heute
  und die Netto-Ergebnisse in R.

Einstellungen (Zahnrad am Script):
- **Engine:** Zeitfenster für Einstiege, Chance-Risiko-Verhältnis, max. Trades pro Tag
  und die Uhrzeit, zu der glattgestellt wird (Standard: 15:55 New York).
- **Risiko:** wie viel Prozent vom Konto pro Trade riskiert werden.
- **Anzeige:** einzelne Ebenen ein- und ausblenden.
- **Alarme:** welche Meldungen kommen sollen.

## 3. Alarm anlegen: 24/7 aufs Handy

Für **jede Aktie** brauchst du einen eigenen Alarm. Ein Alarm gilt immer für ein Symbol
und eine Zeiteinheit.

1. Den Chart auf die gewünschte Aktie und **5 Minuten** stellen.
2. Den **Wecker** oben (oder `Alt + A`) anklicken.
3. **Bedingung:** „Aktien Data Farm“ wählen und darunter
   **„alert() function calls only“** (deutsch sinngemäß: „nur alert()-Funktionsaufrufe“).
   - Wähle **nicht** „Order fills …“, sonst kommt jede Meldung doppelt.
4. **Ablauf:** Bei Premium und Ultimate „unbefristet“ wählen. Bei Essential und Plus
   laufen Alarme nach spätestens **2 Monaten** ab und müssen neu angelegt werden.
5. **Benachrichtigungen:** Den Haken bei **„In der App benachrichtigen“** setzen.
6. **Erstellen.** Danach für die nächste Aktie wiederholen.

**Am Handy:** Die TradingView-App installieren, mit demselben Konto anmelden und
Push-Mitteilungen erlauben.
- iPhone: Einstellungen → Mitteilungen → TradingView
- Android: in den App-Einstellungen

**Wichtig:** Ein Alarm speichert eine Kopie des Scripts und seiner Einstellungen. Wenn du am
Script oder an den Einstellungen etwas änderst, musst du den Alarm **löschen und neu anlegen**.

### Diese Meldungen bekommst du

| Meldung | Bedeutung |
|---|---|
| `ADF NVDA · SHORT-Setup: Sweep EQH 131.20 → MSS 130.70 → FVG · Limit 130.85 · SL 131.40 · TP 129.75` | Setup ist scharf, die Limit-Order wartet |
| `ADF NVDA · SHORT Einstieg @ 130.85 · SL 131.40 · TP 129.75` | Limit wurde erreicht, du wärst im Trade |
| `ADF NVDA · SHORT Limit 130.85 streichen (kein_retest)` | Setup verfallen, die Order löschen |
| `ADF NVDA · SHORT geschlossen @ 129.75 · +2.00R (netto +1.93R)` | Stop oder Ziel erreicht |
| `ADF NVDA · SHORT zum Schluss glattstellen @ 130.10 · +1.36R brutto` | Position vor Börsenschluss schließen |

Optional kannst du unter „Alarme“ auch **jeden Sweep** melden lassen. Für Webhooks gibt es
dort außerdem eine **JSON-Ausgabe**.

### Wann kommen Meldungen?

Die Strategie handelt nur in der regulären US-Handelszeit (9:30–16:00 New York):

- **15:30–22:00 Uhr** deutscher Zeit, Einstiege ab 15:45 Uhr
- Ende Oktober/Anfang November und im März stellen USA und Europa die Uhren an
  verschiedenen Tagen um. Dann ist es für 1–3 Wochen jeweils **eine Stunde früher**.
  2026 betrifft das den 25.10. bis 31.10.

Die Meldungen kommen zum Kerzenschluss, also manchmal ein paar Sekunden später.

## 4. Was dein TradingView-Abo dafür bedeutet

Stand der TradingView-Webseite am 12.10.2026, bitte im eigenen Konto prüfen:

| Abo | Alarme auf Scripts | Läuft ab nach | Watchlist-Alarme |
|---|---|---|---|
| Basic (kostenlos) | keine | – | keine |
| Essential | 20 | 2 Monaten | keine |
| Plus | 100 | 2 Monaten | keine |
| Premium | 400 | unbefristet möglich | 2 |
| Ultimate | 1.000 | unbefristet möglich | 15 |

- **Watchlist-Alarme** (ein Alarm für eine ganze Liste von Aktien) gibt es erst ab Premium.
  Ob sie mit Strategie-Scripts wie diesem funktionieren, ist offiziell nicht bestätigt.
  Ein Alarm pro Aktie funktioniert in jedem Fall.
- Ein Alarm, der mehr als **15-mal in 3 Minuten** auslöst, wird von TradingView gestoppt.
  Das passiert mit diesem Script praktisch nie.
- **Kursdaten:** US-Aktien laufen standardmäßig in Echtzeit über Cboe (kostenlos).
  Die Kurse können minimal von NASDAQ/NYSE abweichen. Exakte Börsendaten kosten extra,
  etwa 3 $ pro Börse und Monat.
- **Webhooks** (Meldungen automatisch an andere Programme schicken) gibt es ab Essential.
  Dafür muss die Zwei-Faktor-Anmeldung aktiv sein.

### Mit Essential (kleinstes bezahltes Abo)

Für dieses Script reicht Essential. Es gibt nur drei Dinge zu beachten:

- **Alarme:** Die 5 Standard-Aktien (SPY, QQQ, AAPL, NVDA, TSLA) brauchen 5 von deinen
  20 Alarmen. Bis zu 20 Aktien gehen also gleichzeitig.
- **Ablauf nach 2 Monaten:** Beim Anlegen das späteste mögliche Ablaufdatum wählen und
  einen Tag vorher eine Erinnerung in den Handy-Kalender eintragen. Dann im
  Alarm-Manager jeden Alarm bearbeiten und ein neues Datum setzen, oder ihn neu anlegen.
  Ein abgelaufener Alarm meldet nichts mehr, ohne dass du es merkst.
- **Backtest-Länge:** Essential lädt 10.000 Kerzen. Auf 5 Minuten mit regulärer
  Handelszeit (78 Kerzen pro Tag) sind das etwa 128 Handelstage, also rund **6 Monate**.
  Der Strategietester rechnet nur über diesen Zeitraum. Längere Tests
  („Deep Backtesting“) gibt es erst ab Premium.

Ein Indikator pro Chart, 1h-Trend über `request.security` und die Push-Meldungen in der App
funktionieren mit Essential ohne Einschränkung.

## 5. Unterschiede zur Python-Farm

Die Logik ist dieselbe wie in `trading-farm/farmlib`. Ein paar Dinge rechnet TradingView aber anders:

- Liegen Stop und Ziel in derselben Kerze, entscheidet TradingView anhand von
  Eröffnung/Hoch/Tief/Schluss, was zuerst kam. Python nimmt immer den Stop.
- Bei Kurslücken füllt TradingView zum Eröffnungskurs.
- TradingView rechnet zusätzlich mit echtem Kontostand und Stückzahlen. Ist eine Aktie zu teuer
  für das Konto, wird das Setup als `kapital_zu_klein` abgelehnt.

Die Ergebnisse beider Versionen sind deshalb ähnlich, aber nicht auf den Cent gleich.
