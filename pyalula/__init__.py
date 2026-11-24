"""pyalula — Python client for the Alula alarm panel API.

The Home Assistant integration vendors this package at
``custom_components/alula/pyalula`` so a panel install does not depend on a
PyPI release. Keep those two copies identical.
"""

from .client import AlulaClient
from .models import AlarmPanel, ArmState, PanelStatus, Zone, ZoneType

__version__ = "0.2.3"

__all__ = ["AlarmPanel", "AlulaClient", "ArmState", "PanelStatus", "Zone", "ZoneType"]
