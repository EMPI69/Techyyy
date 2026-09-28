"""``python -m caudal_bot [--run | --dry-run | --version]``."""

from .main import main

# Guarded so that merely importing this module (tools, test collection, the dry run's
# import sweep) can never start the CLI.
if __name__ == "__main__":
    main(prog="python -m caudal_bot")
