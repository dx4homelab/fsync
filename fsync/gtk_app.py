"""`fsync gtk` — native Linux panel (P4.3, R11), the third UI surface.

Renders the SAME dream4ui-lite Screens as the TUI and web, through the GTK
backend. Two modes:

  fsync gtk                  normal resizable window, full parity with the
                             TUI/web (plan preview with toggles, progress,
                             actions).
  fsync gtk --always-on-top  compact glanceable panel pinned above other
                             windows (status strip + progress bar + Sync now /
                             dry-run / open-full), for a small state surface.

Always-on-top on GNOME/Wayland: GTK4 has no such API and mutter doesn't do
layer-shell, but a GTK3 window under XWayland can set _NET_WM_STATE_ABOVE via
set_keep_above(), which mutter honors for X11 clients. So the panel forces
GDK_BACKEND=x11 and calls set_keep_above(True); on other setups it degrades to
a normal small window.

The reviewed WebSession state machine (poll/act, lock-guarded) is reused for
the network + state logic; only rendering differs.
"""

from __future__ import annotations

import os
import sys
import threading
import time


def _ensure_gi() -> bool:
    """Make PyGObject importable from a venv by borrowing the system gi
    (same Python ABI). Returns True if gi is available."""
    try:
        import gi  # noqa: F401
        return True
    except ImportError:
        pass
    v = f"{sys.version_info[0]}.{sys.version_info[1]}"
    for base in (f"/usr/lib64/python{v}/site-packages", f"/usr/lib/python{v}/site-packages"):
        if os.path.isdir(os.path.join(base, "gi")):
            sys.path.append(base)
            try:
                import gi  # noqa: F401
                return True
            except ImportError:
                return False
    return False


def run_gtk(args) -> int:
    compact = bool(getattr(args, "always_on_top", False))
    if compact:
        # force XWayland before GTK initialises so set_keep_above is honored
        os.environ.setdefault("GDK_BACKEND", "x11")

    if not _ensure_gi():
        print("fsync gtk requires PyGObject/GTK3 (Fedora: it ships in the base image; "
              "in a venv it is borrowed from the system automatically). gi not found.",
              file=sys.stderr)
        return 2

    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    from dream4devops.ui_lite import Action, Actions, Tone
    from dream4devops.ui_lite.render_gtk import to_gtk

    from . import views
    from .client import DaemonUnavailable, FsyncClient
    from .web import WebSession

    try:
        client = FsyncClient(port=getattr(args, "port", None) or 7444)
    except DaemonUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    session = WebSession(client, getattr(args, "profile", None) or None)

    class Panel(Gtk.Window):
        def __init__(self):
            super().__init__(title="fsync")
            self.compact = compact
            self._stop = threading.Event()
            self.scroller = Gtk.ScrolledWindow()
            self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self.holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.scroller.add(self.holder)
            self.add(self.scroller)
            self._apply_mode()
            self.connect("destroy", self._on_destroy)
            # re-assert keep-above once the window is mapped: some WMs (mutter
            # via XWayland) only honor _NET_WM_STATE_ABOVE after map, not on the
            # pre-show request.
            self.connect("map-event", self._on_map)
            self.rebuild()

        def _on_map(self, *_a) -> bool:
            if self.compact:
                self.set_keep_above(True)
            return False

        def _apply_mode(self) -> None:
            if self.compact:
                self.set_keep_above(True)  # honored under XWayland/mutter (X11 clients)
                self.set_resizable(True)
                self.set_default_size(420, 160)
                # constrain so a long status strip scrolls rather than stretching
                self.set_size_request(300, -1)
                self.scroller.set_max_content_width(560)
            else:
                self.set_keep_above(False)
                self.set_size_request(-1, -1)
                self.set_default_size(780, 580)
                self.set_resizable(True)

        def _on_destroy(self, *_a) -> None:
            self._stop.set()
            Gtk.main_quit()

        # ---------------------------------------------------------- polling
        def start(self) -> None:
            threading.Thread(target=self._poll_loop, daemon=True).start()

        def _poll_loop(self) -> None:
            while not self._stop.is_set():
                try:
                    session.poll()
                except Exception:
                    pass
                GLib.idle_add(self.rebuild)
                self._stop.wait(0.5)

        # ---------------------------------------------------------- actions
        def on_action(self, key: str) -> None:
            if key == "full":
                self.compact = False
                self._apply_mode()
                self.rebuild()
                return
            if key == "compact":
                self.compact = True
                self._apply_mode()
                self.rebuild()
                return
            if key == "q":
                self.close()
                return
            try:
                session.act(key)
            except Exception:
                pass
            self.rebuild()

        # ---------------------------------------------------------- render
        def _screen(self):
            with session.lock:
                m, plan, state = session.mode, session.plan, dict(session.state)
                dry, msg = session.dry, session.msg
                partial, progress = session.plan_partial, session.progress
                ds, peer = session.daemon_status, session.peer_line
            if self.compact:
                return views.compact_screen(ds, progress, m, dry, peer)
            # full parity: reuse the same content screens as TUI/web, plus the
            # strip on top (this is a single scrolling column like the web)
            strip = views.status_strip(ds, peer)
            if m == "no_daemon":
                content = views.message_screen(
                    "fsyncd is not running",
                    f"{msg}\n\nStart it: systemctl --user start fsync-daemon", Tone.bad,
                    [Action(key="p", label="retry", tone=Tone.accent),
                     Action(key="compact", label="compact panel")])
            elif m == "planning":
                content = (views.planning_screen(partial) if partial
                           else views.message_screen("", "Planning… indexing both boxes", Tone.warn))
            elif m == "no_peer":
                content = views.message_screen("", f"Peer {msg} is not reachable.", Tone.bad,
                                               [Action(key="p", label="retry", tone=Tone.accent)])
            elif m == "plan" and plan is not None:
                content = views.plan_screen(plan, state, dry, msg)
                content.blocks.append(Actions(items=[Action(key="compact", label="compact panel",
                                                            tone=Tone.accent)]))
            elif m == "spawning" or (m == "plan" and plan is None):
                content = views.message_screen("", "Starting…", Tone.warn)
            elif m == "error" and not progress:
                content = views.message_screen("", msg or "error", Tone.bad,
                                               [Action(key="p", label="retry", tone=Tone.accent)])
            else:
                content = views.progress_screen(progress or {}, m)
            return views.with_strip(strip, content)  # noqa: F821 (closure names below)

        def rebuild(self) -> bool:
            for child in self.holder.get_children():
                self.holder.remove(child)
            widget = to_gtk(self._screen(), on_action=self.on_action)
            self.holder.pack_start(widget, False, False, 0)
            self.holder.show_all()
            return False  # for idle_add: run once

    win = Panel()
    win.show_all()
    win.start()
    Gtk.main()
    return 0
