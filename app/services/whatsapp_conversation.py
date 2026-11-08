"""Backward-compatible WhatsApp conversation worker import.

The channel-independent booking conversation engine lives in
:mod:`app.services.conversation_engine`.
"""

from app.services.conversation_engine import process_whatsapp_conversation_job

__all__ = ["process_whatsapp_conversation_job"]
