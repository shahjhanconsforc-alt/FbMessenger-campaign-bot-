"""The layer that drives a real browser.

`browser.py` owns Chrome and the thread allowed to talk to it. `x.py` is the one
file that knows what X's pages look like - the part that rots when X ships a new
build, kept together so the repair is one file.
"""

from .browser import Automation, AutomationError, automation

__all__ = ["Automation", "AutomationError", "automation"]
