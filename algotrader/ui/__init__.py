"""Desktop UI (PRD s.12): a PySide6 application that *attaches* to a separately running engine.

Closing the window never stops risk controls or open-trade management: the UI reads the
snapshot the engine publishes to the state database and sends operator commands through a
queue that the engine applies via the risk gateway (``core.state`` command queue).

``bridge`` has no Qt dependency so it can be unit-tested and reused by other front ends.
"""
