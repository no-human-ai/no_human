"""The welcome-email seam: frozen copy + template selection + transport.

``base.py`` is a byte-identical vendored copy of no_human-cloud's
``infra/team-brain/email/base.py`` (see its header) — the subject/body text is
frozen and must never be edited here; any wording change is an upstream
change, copied down again.

``send.py`` is the one module that knows how to pick a template for the
current platform and hand the rendered message to a transport. The operator
chose Resend as the transport (2026-09-12; see send.py's module docstring)
— SES is not used, its production-access request having been denied. With
``RESEND_API_KEY`` set in ``~/.no_human/.env`` the default transport POSTs to
Resend; with no key set it reports ``"not_sent:unconfigured"`` and never
raises. Swapping in a different transport touches only ``send.py``.
"""

from __future__ import annotations
