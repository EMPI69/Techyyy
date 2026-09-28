"""Launcher: ``python bot.py --run`` starts the bot; ``--dry-run`` checks the setup offline.

The bot lives in the caudal_bot package. A launch without --run never connects to Discord.
"""

from caudal_bot.main import main

if __name__ == "__main__":
    main(prog="bot.py")
