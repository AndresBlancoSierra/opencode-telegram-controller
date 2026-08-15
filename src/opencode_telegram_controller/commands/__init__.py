"""Thin aiogram routers for the PC Control commands.

Handlers stay thin: they check permissions, call a capability service, format
the result and reply. All logic lives in the service layer.
"""

from __future__ import annotations
