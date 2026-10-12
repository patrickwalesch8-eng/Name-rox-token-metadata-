# Aktien Data Farm

Ein Analyse- und Backtest-Bot für den Aktienmarkt nach dem Vorbild der „LIT Data Farm“:
Jeder Handelstag wird Bar für Bar abgespielt, Smart-Money-Muster werden erkannt,
und eine Engine entscheidet bei jedem Setup, ob sie es handelt oder ablehnt.
Alles landet in einem Dashboard mit den Tabs **Übersicht, Chart, Studien und Events**.

> **Wichtig:** Das hier handelt **kein echtes Geld**. Es zeigt, wie die Strategie in der
> Vergangenheit abgeschnitten *hätte*. Gute Backtest-Zahlen sind keine Garantie für die Zukunft.
> Bewerte immer die **Netto**-Werte, also nach Abzug von Slippage und Gebühren.

## Am PC starten (Windows)

1. **Python installieren** (einmalig): <https://www.python.org/downloads/>.
   Setze beim Installieren den Haken bei **„Add Python to PATH“**.
2. **Projekt herunterladen:** Auf GitHub *Code → Download ZIP* wählen und entpacken.
3. Den Ordner `trading-farm` öffnen und **`start.bat` doppelklicken**.

Beim ersten Start wird alles eingerichtet (1–2 Minuten). Danach lädt das Skript die
Kursdaten und öffnet das Dashboard im Browser. Mit diesem Doppelklick kannst du es
jederzeit aktualisieren.

Unter macOS oder Linux nimmst du stattdessen `./start.sh`.

### Einzelne Befehle

```
python farm.py fetch      # neue 5-Minuten-Daten von Yahoo Finance laden
python farm.py run        # Erkennung + Engine laufen lassen, Dashboard bauen
python farm.py serve      # Dashboard im Browser öffnen
python farm.py all        # alles nacheinander
python farm.py demo       # Demo mit künstlichen Kursen (ohne Internet)
python farm.py fetch --symbols AAPL MSFT AMD   # andere Aktien
```

Yahoo liefert 5-Minuten-Daten nur für die **letzten ~60 Tage**. Jeder `fetch` hängt
die neuen Tage an die gespeicherten an (`data/raw/`), sodass die Historie mit der Zeit wächst.

## Am Handy ansehen

Das Dashboard kann sich automatisch jeden Abend nach US-Börsenschluss aktualisieren
und auf einer Webseite erscheinen. Dafür sind einmalig zwei Schritte nötig:

1. Den Branch mit diesem Code in `main` mergen, denn GitHub führt geplante Abläufe nur dort aus.
2. Auf GitHub unter **Settings → Pages → Source** die Option **„GitHub Actions“** wählen.

Danach unter **Actions → Trading Farm → Run workflow** einmal manuell starten.
Das Dashboard erscheint dann unter
`https://patrickwalesch8-eng.github.io/Name-rox-token-metadata-/`.
Diese Adresse kannst du als Lesezeichen auf dem Homescreen speichern.

Hinweise:
- Das Repo ist öffentlich, also ist auch diese Seite öffentlich. Es sind nur Kursdaten und Statistiken zu sehen, keine Kontodaten.
- Yahoo blockiert Server-Anfragen gelegentlich. Dann zeigt die Seite einfach den letzten Stand.

## TradingView-Version (Alarme rund um die Uhr)

Im Ordner [`tradingview/`](tradingview/) liegt dieselbe Strategie als Pine Script für
TradingView. Sie zeichnet alle Muster in den Chart und läuft im Strategietester. Die Alarme
laufen auf den TradingView-Servern, also auch wenn dein PC aus ist.
Die Anleitung steht in [`tradingview/ANLEITUNG.md`](tradingview/ANLEITUNG.md).

## Was die Farm erkennt

| Event | Bedeutung |
|---|---|
| `swing` | bestätigte Hochs/Tiefs, beschriftet HH / HL / LH / LL |
| `pool_formed` | Liquidität: gleiche Hochs/Tiefs (EQH/EQL), Vortageshoch/-tief (PDH/PDL) |
| `sweep` | Docht durch einen Pool, Schluss wieder zurück (Stopps abgeholt) |
| `pool_taken` | Pool mit Schlusskurs durchbrochen |
| `bos` / `mss` | Strukturbruch mit / gegen den Trend |
| `fvg_formed`, `fvg_tapped`, `fvg_inverted` | Fair Value Gaps und was danach mit ihnen passiert |
| `ob_formed`, `ob_mitigated`, `ob_invalidated` | Order Blocks und was danach mit ihnen passiert |

Alles wird auf 5m, 15m und 1h erkannt. Ein Event wird erst auf der Kerze gemeldet,
auf der es wirklich feststeht, also **ohne Blick in die Zukunft**. Die Tests prüfen genau das.

## Die Engine (das Setup)

1. Ein Liquiditäts-Pool wird **gesweept**. Bei einem BSL-Sweep sucht die Engine einen Short, bei einem SSL-Sweep einen Long.
2. Innerhalb von `mss_window` Kerzen muss ein **MSS/BOS in Gegenrichtung** kommen.
3. Die Einstiegszone ist das neueste **FVG** dieser Bewegung, sonst der **Order Block**.
4. Eine **Limit-Order** wartet `entry_window` Kerzen am Rand der Zone.
5. Der **Stop** liegt hinter dem Sweep-Docht, das **Ziel** bei `target_rr` × Risiko.
   Spätestens mit der Kerze, die um `flat_at` (15:55 New York) endet, wird glattgestellt.

Filter, die zu einer Ablehnung führen können: 1h-Trend dagegen, Handelszeitfenster,
Risiko zu groß oder zu klein, Tageslimit, Position schon offen.
Jede Ablehnung wird mit ihrem Grund gespeichert.

Die Fills sind bewusst pessimistisch: Liegen Stop und Ziel in derselben Kerze,
zählt der Stop, und Kurslücken bringen keinen besseren Preis.

Alle Parameter stehen in **`config.json`**: Aktienliste, Zeitfenster, Chance-Risiko-Verhältnis,
Slippage und Gebühren. Nach einer Änderung einfach erneut `python farm.py run` ausführen.

## Tests

```
python -m unittest discover -s tests
```
