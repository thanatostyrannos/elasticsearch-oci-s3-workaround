"""Entry point: `python3 -m generation_chain`.

This package is a directory rather than a single file, which is a deliberate
exception to this project's one-tool-one-file rule. Deploying it means copying
a directory to the jump host instead of a file. There is no install step and
no third-party package, so a copied directory runs as it stands.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
