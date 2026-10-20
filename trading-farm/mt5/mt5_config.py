"""
Einstellungen des MT5-Autopiloten der Aktien Data Farm.

Die STRATEGIE (Sweep -> MSS -> FVG/OB, Zeitfenster, Chance-Risiko usw.) steht in
../config.json und ist dieselbe wie im Dashboard und im TradingView-Script.
Hier steht nur, WIE und WO gehandelt wird.

WICHTIG: Der Bot ist für DEMO-KONTEN gebaut. ALLOW_REAL_ACCOUNT steht auf False,
dann verweigert der Bot den Start auf einem Echtgeldkonto. Ändere das erst, wenn
das System über Wochen im Demo-Betrieb bewiesen hat, dass es sich wie erwartet
verhält - und auch dann nur mit Geld, dessen Verlust du verkraften kannst.
"""

# --- Sicherheit ---
ALLOW_REAL_ACCOUNT = False   # NIEMALS leichtfertig auf True setzen
MAGIC_NUMBER = 200002        # Kennung der Orders dieses Bots (anders als beim Trend-Bot: 100001)

# --- Login (leer lassen, wenn das MT5-Terminal schon eingeloggt ist) ---
MT5_LOGIN = None             # z.B. 12345678 (Demo-Kontonummer)
MT5_PASSWORD = None
MT5_SERVER = None            # z.B. "MetaQuotes-Demo"
# Nur nötig, wenn mehrere MetaTrader 5 installiert sind: welches Terminal der Bot nimmt
MT5_PATH = None              # z.B. r"C:\Program Files\MetaTrader 5\terminal64.exe"

# --- Märkte: Name in der Farm -> Symbolname bei DEINEM Broker ---
# Jeder Broker benennt Aktien-CFDs anders, z.B. "NVDA", "NVDA.US", "#NVDA",
# "NVDA.NAS". Findet der Bot ein Symbol nicht, zeigt er dir passende Namen an.
MARKETS = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "AAPL": "AAPL",
    "NVDA": "NVDA",
    "TSLA": "TSLA",
}

# --- Risikomanagement ---
RISK_PERCENT = 1.0             # % des Kapitals, das pro Trade riskiert wird
MAX_OPEN_POSITIONS = 3         # offene Positionen + wartende Limit-Orders zusammen
DAILY_LOSS_LIMIT_PERCENT = 3.0 # Tagesverlust (vom Kapital zu Tagesbeginn), ab dem für
                               # den Rest des Tages keine neuen Orders mehr kommen (0 = aus)
MAX_SPREAD_OF_RISK = 0.20      # Spread darf höchstens 20 % des Stop-Abstands sein

# --- Zeit ---
# Nach welcher Regel läuft die Uhr deines Brokers? MT5 liefert alle Kerzen in Serverzeit,
# und viele Broker stellen auf Sommer-/Winterzeit um. Die Regel gilt für jede Kerze einzeln.
#   "NY+7"          Serverzeit = New-York-Zeit + 7 h  (GMT+2 im Winter / GMT+3 im Sommer,
#                   Umstellung mit den USA - bei den meisten MT5-Brokern so)
#   "Europe/Athens" GMT+2/+3 mit EUROPÄISCHER Umstellung
#   "UTC+2"         feste Verschiebung ohne Umstellung
# Der Bot prüft die Regel laufend an den Live-Kursen. Passt sie nicht, meldet er die
# gemessene Serverzeit und platziert keine Orders, bis du sie hier korrigiert hast.
SERVER_TZ = "NY+7"
CLOCK_SYMBOLS = ["EURUSD"]     # Symbole mit fast durchgehenden Kursen für die Zeitprüfung

# --- Technik ---
BARS_HISTORY = 6000            # wie viele 5-Minuten-Kerzen geladen werden (~4-8 Wochen)
POLL_SECONDS = 10              # wie oft nach neuen Kerzen geschaut wird
LOG_FILE = "bot_smc.log"
JOURNAL_FILE = "trades_smc.csv"
STATE_FILE = "bot_smc_state.json"

# --- Anzeige ---
HEARTBEAT_MINUTES = 15         # alle N Minuten eine Statuszeile pro Markt
