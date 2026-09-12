"""The welcome email for someone who registered INSIDE the running app.

`base.py` is vendored byte-identical from no_human-cloud and its four templates
serve the WEBSITE flows: a visitor asks for a download, or joins a waitlist.
Those are wrong here, and were being sent here. This step fires inside the
already-installed desktop app, after the user has downloaded it, installed it,
opened it and walked into setup — so the macOS template's

    "Here's your download — signed and notarized..."
    "Open the DMG and drag no_human to Applications"
    "You're receiving this because you requested this download at getnohuman.com."

are all false at the moment they arrive: they are read by someone looking at the
running application, told to go install the thing they are using, and given a
reason for the email that never happened.

The voice is deliberately NOT re-invented. `_greet`, `_INTRO`, `_OUTRO` and
`_footer` are imported from `base`, so this reads as the same person as the
other three and follows any future change to the operator-approved copy. Only
the middle paragraph and the footer's `why` differ, because only those two
things are actually different about this reader.

No download link, and no install instructions: they already did that. No
Discord link either -- the invite has exactly one source in this repo
(`web/src/community.js`), the onboarding step offers it, and a second literal
here would be a second thing to keep in sync for no gain.
"""

from __future__ import annotations

from .base import SITE, _footer, _greet, _INTRO, _OUTRO


def in_app_welcome(unsubscribe_url: str, email: str = "") -> tuple[str, str]:
    """Subject and body for an address registered during in-app setup."""
    subject = "You're set up with no_human"
    text = f"""{_greet(email)}

{_INTRO}

You've got it installed and running, so I'll skip the setup
advice. If you want to go deeper the docs are at {SITE}/docs —
the part worth reading first is how to write acceptance criteria
a reviewer can actually check.

{_OUTRO}"""
    return subject, text + _footer(
        "You're receiving this because you registered this address while "
        "setting up no_human.", unsubscribe_url)
