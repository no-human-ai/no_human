"""The welcome-email seam: frozen copy + template selection + transport.

``base.py`` is a byte-identical vendored copy of no_human-cloud's
``infra/team-brain/email/base.py`` (see its header) — the subject/body text is
frozen and must never be edited here; any wording change is an upstream
change, copied down again.

``send.py`` is the one module that knows how to pick a template for the
current platform and hand the rendered message to a transport. Delivery is
NOT wired today (SES is still in sandbox mode — see send.py's module
docstring); the default transport always reports ``"not_sent:unconfigured"``
and never raises. Swapping in a real transport touches only ``send.py``.
"""

from __future__ import annotations
