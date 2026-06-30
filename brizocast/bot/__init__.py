"""Presentation layer — thin Telegram adapters only.

Contains command/conversation handlers, inline keyboard builders, and message
formatters. No business rules live here; handlers parse input, call a service,
and format a reply, keeping Telegram swappable and the logic testable headlessly.
"""
