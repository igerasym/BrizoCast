"""Domain core — pure, framework-free.

Holds the dependency-injection container, port interfaces (``ports``), pure
domain value objects and logic (``domain``), and the domain error hierarchy.
This layer knows nothing about Telegram, SQLAlchemy, HTTP, or APScheduler.
"""
