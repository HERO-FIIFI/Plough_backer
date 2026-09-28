"""Signal-provider profiles: one module per provider, picked up automatically.

New provider: copy loverock_fx.py to <name>.py, edit REWRITES (and bump VERSION), then set
`parser_profile: <name>` on the source in config/sources.yaml. REWRITES turn the provider's
text into the standard_v1 format ("XAUUSD BUY 4300", "SL 4290", "TP1 4306"); the standard
parser then applies every safety rule unchanged, so a bad rewrite is rejected, never traded.
"""
