"""
version.py
Single source of truth for the app version. The patch (third) number is
bumped on every change set. Read in:
  - web_dashboard.py (rendered in the topbar)
  - any future status / about endpoint
"""

__version__ = "1.0.103"
