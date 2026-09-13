# Vendored byte-identical from no_human-cloud infra/team-brain/email/base.py
# (also duplicated byte-identically as download_lambda/base.py and
# waitlist_lambda/base.py in that repo). Do not reformat or reword: the
# body text below is the operator-approved founder email, frozen 2026-08-16.
# Upstream: no_human-cloud/infra/team-brain/email/base.py
"""The three transactional emails — plain text, from the founder.

Operator-directed 2026-08-16: these are PERSONAL emails from Eyal, in regular
plaintext, built around the operator's own copy, and they should encourage a
reply. That direction happens to match what the exemplar research found
performs best for developer tools anyway (the Linear pattern: reads like a
note from a person, not a template) — and text-only sidesteps every HTML
client hazard the earlier design had to engineer around.

What each send still carries, non-negotiably:
- the visible unsubscribe line in the footer AND the RFC 8058 one-click
  List-Unsubscribe headers (send.py adds those);
- a why-you-got-this line;
- one clear action (the download link / the reply prompt) near the top.

`greeting_name` is best-effort from the address's local part: "dana.lee@x.io"
→ "Dana" beats "Hi," and lying beats neither — when the local part is
noise (uuid-ish, no letters), the greeting falls back to plain "Hi,".
"""

from __future__ import annotations

import re

SITE = "https://getnohuman.com"
_NAME_RE = re.compile(r"^[a-zA-Z]{2,}")


def greeting_name(email: str) -> str:
    local = email.split("@", 1)[0]
    first = re.split(r"[._+-]", local, 1)[0]
    if _NAME_RE.match(first) and not first.isdigit():
        return first.capitalize()
    return ""


def _greet(email: str) -> str:
    name = greeting_name(email)
    return f"Hi {name}," if name else "Hi,"


_INTRO = "I'm Eyal, the founder of no_human."

_OUTRO = """I built no_human because I got tired of babysitting Claude. Are you sick of that too?

Let me know if no_human helped.

I would really love any feedback — just hit reply, I read every one.

Happy coding,
Eyal"""


def _footer(why: str, unsubscribe_url: str) -> str:
    return f"""

--
{why}
no_human · {SITE}
Unsubscribe: {unsubscribe_url}"""


def mac_download(download_url: str, unsubscribe_url: str,
                 email: str = "") -> tuple[str, str]:
    subject = "Your no_human download for macOS"
    text = f"""{_greet(email)}

{_INTRO}

Here's your download — signed and notarized, so macOS opens it
without ceremony:

{download_url}

Open the DMG and drag no_human to Applications (Apple silicon,
macOS 13 or newer). First run walks you through connecting your own
Claude subscription; the quickstart is at {SITE}/docs if you want it.

{_OUTRO}"""
    return subject, text + _footer(
        "You're receiving this because you requested this download at "
        "getnohuman.com.", unsubscribe_url)


def linux_download(download_url: str, unsubscribe_url: str,
                   email: str = "", fmt: str = "deb") -> tuple[str, str]:
    """The Linux twin of `mac_download`.

    Two formats, two sets of install lines, because the frictions differ and
    both were observed on a real Ubuntu 24.04 desktop (docs/LINUX.md §7): the
    .deb is `apt install ./file` (double-clicking hands it to a store app that
    may not offer to install a local file), and the AppImage needs the execute
    bit set before it will start. Saying so here costs three lines and saves
    the first-run support question.
    """
    deb = fmt != "appimage"
    subject = ("Your no_human download for Linux"
               + ("" if deb else " (AppImage)"))
    how = ("""Install it with apt, which pulls in what it needs:

  sudo apt install ./no_human-*-linux-amd64.deb

Then launch no_human from your applications menu. (Double-clicking the
file hands it to your desktop's store app, which does not always offer
to install a local package — the command above always works.)"""
           if deb else
           """Make it executable, then run it:

  chmod +x no_human-*-linux-x86_64.AppImage
  ./no_human-*-linux-x86_64.AppImage

Nothing is installed system-wide; the AppImage is the whole app. There
is no auto-update on Linux yet — new versions come from the site.""")
    text = f"""{_greet(email)}

{_INTRO}

Here's your download for Linux (x86_64):

{download_url}

{how}

First run walks you through connecting your own Claude subscription;
the quickstart is at {SITE}/docs if you want it.

{_OUTRO}"""
    return subject, text + _footer(
        "You're receiving this because you requested this download at "
        "getnohuman.com.", unsubscribe_url)


def windows_waitlist(unsubscribe_url: str, email: str = "") -> tuple[str, str]:
    subject = "You're on the list for no_human on Windows"
    text = f"""{_greet(email)}

{_INTRO}

You're on the list for the Windows build — you'll get exactly one
email from me the day it ships, and nothing before that. (If you
also work on a Mac, no_human runs there today: {SITE})

{_OUTRO}"""
    return subject, text + _footer(
        "You're receiving this because you joined the Windows waitlist at "
        "getnohuman.com.", unsubscribe_url)


def waitlist(unsubscribe_url: str, email: str = "") -> tuple[str, str]:
    subject = "You're on the no_human waitlist"
    text = f"""{_greet(email)}

{_INTRO}

You're on the waitlist — when your access opens you'll get one email
with everything you need, pricing included. No drip before that.

{_OUTRO}"""
    return subject, text + _footer(
        "You're receiving this because you joined the waitlist at "
        "getnohuman.com.", unsubscribe_url)
