"""Launch GARGAND from the nested bot directory used by the current Fadehost build."""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(ROOT, "archive-2026-10-04T205709Z")
if not os.path.isdir(BOT_DIR):
    raise RuntimeError("GARGAND bot directory is missing: " + BOT_DIR)
# Match the active build directory so its existing SQLite database and config stay in use.
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)
sys.path.insert(1, ROOT)

import main
import nightfall_upgrades
import gargand_features

# Register extensions during discord.py's async setup phase, when an event loop is active.
_original_setup_hook = main.bot.setup_hook

async def gargand_setup_hook():
    await _original_setup_hook()
    nightfall_upgrades.install(main)
    gargand_features.install(main)

main.bot.setup_hook = gargand_setup_hook
main.bot.run(main.TOKEN)
