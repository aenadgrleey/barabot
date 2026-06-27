"""Reporter bot — sends 'NEW item' notifications and answers /commands.

Talks to the same SQLite as barabot (read-only for items table; it does
not write to the embeddings). Uses Bot API via pyrogram (bot_token).
"""
__version__ = "0.1.0"
