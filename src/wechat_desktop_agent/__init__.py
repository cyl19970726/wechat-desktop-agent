"""Offline, supervised desktop delivery state. No WeChat integration."""

from .outbox import (DesktopDriver, FullTarget, Observation, Outbox, ReceiptEvidence,
                     ReceiptVerifier, Refused, Session, WindowIdentity)

__all__ = ["DesktopDriver", "FullTarget", "Observation", "Outbox", "ReceiptEvidence",
           "ReceiptVerifier", "Refused", "Session", "WindowIdentity"]
