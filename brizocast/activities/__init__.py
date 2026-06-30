"""Per-activity plug-ins built around the Activity abstraction.

An ``ActivityRegistry`` maps an activity key to an ``Activity`` carrying its
``Scorer``, condition schema, and provider binding. Adding a sport means adding
a package here and calling ``register()`` — no edits to existing code.
"""
