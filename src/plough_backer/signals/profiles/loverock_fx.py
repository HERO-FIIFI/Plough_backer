"""LOVEROCK FX (Telegram). Sample:

    📊XAUUSD SELL NOW
    ( 4202 ) ✅
    📊TARGET 1  ( 4198 )✅
    📊TARGET 2  ( 4194 )✅
    🚫 STOP LOSS   ( 4214 )
    RISK MANAGEMENT IS IMPORTANT ✅

Follow-ups ("Tp1 hit", "Sl hit", "All tp hit") have no BUY/SELL and are rejected as
non-signals by the standard parser.
"""

VERSION = "loverock_fx.1"

# (regex, replacement), applied in order with re.MULTILINE | re.IGNORECASE.
REWRITES: list[tuple[str, str]] = [
    (r"[^A-Za-z0-9.\n ]", " "),  # drop emojis, ticks, brackets
    (r"^[ ]*TARGET[ ]*(\d)[ ]+", r"TP\1 "),  # "TARGET 1 4198" -> "TP1 4198"
    (r"^[ ]*STOP[ ]*LOSS[ ]+", "SL "),  # "STOP LOSS 4214" -> "SL 4214"
    (r"\b(BUY|SELL)[ ]+NOW[ ]*\n[ ]*(\d+(?:\.\d+)?)[ ]*$", r"\1 \2"),  # entry on next line
]
