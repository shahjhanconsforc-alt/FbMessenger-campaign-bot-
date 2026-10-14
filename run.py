"""Start the console, from the repo root.

    python run.py

The whole of it is `backend/run.py`, which already puts the repo root on the path
and serves the API and the console together. This file exists so the command is
the short one from the folder the project is opened in, rather than one that
carries the `backend/` path a person has to remember to type.
"""

from __future__ import annotations

from backend.run import main

if __name__ == "__main__":
    main()
