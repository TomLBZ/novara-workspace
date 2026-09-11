"""ws-write plugin - file maintenance for the /workspace bind mount.

Hermes denies ``write_file``/``patch`` outside ``HERMES_WRITE_SAFE_ROOT``
(``/opt/data`` in this container), so the bind-mounted workspace is read-only to
those tools. This plugin registers two tools in the plugin-owned ``workspace``
toolset that write through plain Python I/O:

* ``ws_write`` - atomic create/replace/append
* ``ws_patch`` - exact-substring edit (the ``patch`` analogue)

Install: ``/workspace/bin/ws-plugin-install`` (copies the plugin into
``$HERMES_HOME/plugins`` and enables it). Loading happens at gateway start, so a
restart is required. Canonical source: ``/workspace/tools/hermes-plugins/ws-write``.
"""
from __future__ import annotations

from .tools import SCHEMAS, ws_patch, ws_write

TOOLSET = "workspace"
_HANDLERS = {"ws_write": ws_write, "ws_patch": ws_patch}


def register(ctx) -> None:
    """Register both tools; ``ctx.register_tool`` is the supported plugin surface."""
    for name, handler in _HANDLERS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=SCHEMAS[name],
            handler=handler,
            description=SCHEMAS[name]["description"],
        )
