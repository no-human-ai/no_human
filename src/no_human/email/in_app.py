"""The welcome email for someone who registered INSIDE the running app.

`base.py` is vendored byte-identical from no_human-cloud and its four templates
serve the WEBSITE flows: a visitor asks for a download, or joins a waitlist.
Those are wrong here, and were being sent here. This step fires inside the
already-installed desktop app, after the user has downloaded it, installed it,
opened it and walked into setup — so the macOS template's "Here's your
download", "Open the DMG and drag no_human to Applications" and "because you
requested this download at getnohuman.com" are all false at the moment they
arrive.

WHY THIS RENDERS HTML AS WELL AS TEXT (operator direction, 2026-09-12). The
other four templates are plain text only. This one is the first thing a new
user sees from us, so it carries a designed HTML part in the board's own
palette; the text part is not a stub — it is the same copy, from the same
constants below, and is what a text-only client shows.

The copy lives in ONE place: every string a reader sees is a module constant.
What a test pins, precisely, is that each constant in `SHARED_COPY` reaches
BOTH parts, and that the HTML's visible sentences are exactly the constants
this module declares -- so copy cannot be added to one part alone, and cannot
be smuggled into markup where no reader sees it. It is NOT a claim that the
two parts are identical: `EYEBROW` and `HEADLINE` are HTML-only chrome and are
deliberately absent from the text part, which is why they are listed
separately. Changing a constant's WORDING is an ordinary copy edit and stays
green; that is the intended latitude, not a gap.

PALETTE. Email has no CSS cascade and no custom properties — `var(--base)`
resolves to nothing in every mail client — so the tokens are repeated here as
literals. DESIGN.md's "a one-off literal in a component is a defect" is about
`web/src/`, where a variable exists to be used; here the honest equivalent is a
single table keyed BY THE TOKEN NAME, which a test compares against
`web/src/styles.css` so a palette change cannot silently leave this file behind.

FONTS degrade on purpose. DM Sans and IBM Plex Mono are bundled for the board
(`web/src/assets/fonts/`) and deliberately never fetched from a CDN; an email
cannot bundle them and must not fetch them, so each stack names the brand face
first — used only if the reader's machine already has it — and falls back to
system faces. No @font-face, no remote asset, no tracking pixel, no image: the
wordmark is text.
"""

from __future__ import annotations

import html as _html
import textwrap

from .base import SITE, _footer, _greet, _INTRO

# ── Copy: the single source for BOTH the text and the HTML part ──────────────
SUBJECT = "Welcome to no_human"
#: The wordmark at the top of the card. A constant, not an inline literal,
#: because the module claims every string a reader sees is one — and a
#: reader-visible literal is exactly the seam the drift test cannot police.
#: Brand rule (DESIGN.md): lowercase with the underscore, always.
WORDMARK = "no_human"
#: The footer's unsubscribe LABEL. base.py's frozen `_footer` spells the
#: mailto out in the text part; the HTML part needs a link label, and that
#: label is copy like any other.
UNSUBSCRIBE_LABEL = "Unsubscribe"
#: The footer's separator between the wordmark and the site link. A constant
#: for the same reason as the two above: it is a character a reader sees, and
#: the drift test can only police what it can name.
FOOTER_SEP = "\u00b7"
#: Where the CTA points. A page on the SITE, not on this install, because a
#: handoff served by the app it launches is dead whenever the app is closed.
#:
#: EXTERNAL DEPENDENCY, stated because nothing in this repo can satisfy it:
#: the page is served by the `no_human-site` repo, not by us, so the button is
#: only as good as that deployment. No test here can catch its removal.
#:
#: Measured against the live site after deploying it, 2026-09-13:
#:     /open        -> 200
#:     /open.html   -> 404
#: Note the second row. The `*.html -> /x` 307 that `/about.html` and
#: `/docs.html` get does NOT apply to this page, so the extensionless spelling
#: is not merely canonical here, it is the only one that resolves. An earlier
#: revision of this comment claimed the 307 held for `/open.html` on the
#: strength of having measured it on `/about.html` -- a convention observed
#: elsewhere is not a measurement of this URL.
OPEN_URL = "https://getnohuman.com/open"
EYEBROW = "WELCOME"
HEADLINE = "Welcome to no_human."
#: Three sentences, and every one of them has to be true for EVERY install.
#: Not "on your own Claude subscription" — `llm.auth_mode: "api_key"` is a
#: sanctioned mode (CLAUDE.md constraint #1) and that sentence would be false
#: for those users. "on your own account" holds in both. The middle sentence is
#: constraint #2 — the agent opens a PR and stops — stated as the user
#: experiences it.
BODY = (
    "You're set up: no_human is running on your machine, on your own "
    "account. Everything it does lands as a branch and a pull request you "
    "review — nothing merges without you. Start with one small task and "
    "see what comes back."
)
CTA_LABEL = "Open no_human"
SIGNOFF = "— Eyal, founder"
REPLY_NOTE = "Questions, ideas, something broken? Just hit reply. I read every one."
WHY = ("You're receiving this because you registered this address while "
       "setting up no_human.")

#: Copy every reader sees, in BOTH the text and the HTML part.
SHARED_COPY = ("BODY", "CTA_LABEL", "SIGNOFF", "REPLY_NOTE", "WHY")
#: HTML-only chrome. A text email has no eyebrow and no display headline, so
#: these are deliberately absent from the text part rather than missing from it.
HTML_ONLY_COPY = ("EYEBROW", "HEADLINE")

#: Board tokens, by their `web/src/styles.css` `:root` names. Verified against
#: that file by tests/test_onboarding_email.py — do not edit one side alone.
PALETTE = {
    "base": "#0F1117",        # canvas
    "surface-1": "#1A1D27",   # the card
    "border": "#2E3241",
    "text-hi": "#E8ECF2",     # headline
    "text": "#C9CDD6",        # body
    "text-muted": "#A8AFC5",  # the reply note
    "text-dim": "#8C96B2",    # the why/unsubscribe footer
    "accent-500": "#4C9AFF",  # eyebrow + button fill
}
_SANS = ("'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "Helvetica,Arial,sans-serif")
_MONO = ("'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace")


def _text_part(unsubscribe_url: str, email: str) -> str:
    """The plain-text part: same copy, wrapped, in base.py's own shape."""
    return f"""{_greet(email)}

{_INTRO}

{textwrap.fill(BODY, 72)}

{CTA_LABEL}: {OPEN_URL}

{REPLY_NOTE}

{SIGNOFF}""" + _footer(WHY, unsubscribe_url)


def _html_part(unsubscribe_url: str, email: str) -> str:
    """The HTML part: a single centred card on the board canvas.

    Table-based and fully inlined because that is what survives the clients
    people actually read mail in: Gmail strips <style> blocks, and Outlook's
    Word engine ignores CSS backgrounds on divs (hence `bgcolor` attributes
    alongside every background) and drops border-radius (the pill degrades to
    a rectangle, which is fine — no VML hack).

    The TWO RUNTIME INPUTS are escaped -- the greeting and the unsubscribe URL
    -- because those are the only values here that come from outside this
    module. `greeting_name` derives the greeting from the address's local part
    and does NOT sanitise: measured,
    `greeting_name("dana<script>alert(1)</script>@x.io")` returns
    'Dana<script>alert(1)</script>'. Harmless while the body was text; an
    injection the moment it is markup.

    The CTA's href used to be a third: it was a URL the caller supplied, built
    from this install's own `server.host`/`server.port`. It is now `OPEN_URL`,
    a literal in this file, so it joins the class below rather than the class
    above -- there is no longer any path by which a config value, a CLI flag
    or an API caller can decide where the button points.

    The copy constants and the palette are NOT escaped: they are trusted
    literals in this file. That is a constraint on editing them, and it is
    position-dependent: a constant landing in TEXT CONTENT must carry no `<`,
    `>` or `&`, and one landing inside a double-quoted attribute (`OPEN_URL`,
    `SITE`) must additionally carry no `"`. An apostrophe is safe in both, and
    is ordinary copy -- "You're set up" is BODY's first word. A test enforces
    exactly that, per position, rather than leaving it as a note here; if a
    constant ever needs one of those characters, escape at its interpolation
    site instead of relaxing the rule.
    """
    p = PALETTE
    greet = _html.escape(_greet(email))
    unsub = _html.escape(unsubscribe_url, quote=True)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
<title>{_html.escape(SUBJECT)}</title>
</head>
<body style="margin:0;padding:0;background:{p['base']};color:{p['text']}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 bgcolor="{p['base']}" style="background:{p['base']}">
<tr><td align="center" style="padding:40px 20px">

<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
 style="width:560px;max-width:100%">

<tr><td align="center" style="padding-bottom:26px;font-family:{_MONO};
 font-size:19px;letter-spacing:1px;color:{p['text-hi']}">{WORDMARK}</td></tr>

<tr><td bgcolor="{p['surface-1']}" style="background:{p['surface-1']};
 border:1px solid {p['border']};border-radius:14px;padding:40px 36px">
 <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">

 <tr><td align="center" style="font-family:{_SANS};font-size:11px;font-weight:700;
  letter-spacing:1.6px;color:{p['accent-500']};padding-bottom:12px">{EYEBROW}</td></tr>

 <tr><td align="center" style="font-family:{_SANS};font-size:25px;font-weight:700;
  line-height:1.3;color:{p['text-hi']};padding-bottom:16px">{HEADLINE}</td></tr>

 <tr><td align="center" style="font-family:{_SANS};font-size:15px;line-height:1.65;
  color:{p['text']};padding-bottom:14px">{greet} {_INTRO}</td></tr>

 <tr><td align="center" style="font-family:{_SANS};font-size:15px;line-height:1.65;
  color:{p['text']};padding:0 24px 30px">{BODY}</td></tr>

 <tr><td align="center" style="padding-bottom:28px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
  <td bgcolor="{p['accent-500']}" style="background:{p['accent-500']};
   border-radius:999px"><a href="{OPEN_URL}"
   style="display:inline-block;padding:14px 32px;font-family:{_SANS};font-size:15px;
   font-weight:700;color:{p['base']};text-decoration:none">{CTA_LABEL}</a></td>
  </tr></table>
 </td></tr>

 <tr><td align="center" style="font-family:{_SANS};font-size:14px;
  color:{p['text']};padding-bottom:18px">{SIGNOFF}</td></tr>

 <tr><td align="center" style="font-family:{_SANS};font-size:13px;line-height:1.6;
  color:{p['text-muted']}">{REPLY_NOTE}</td></tr>

 </table>
</td></tr>

<tr><td align="center" style="padding-top:24px;font-family:{_SANS};font-size:12px;
 line-height:1.7;color:{p['text-dim']}">
 {WHY}<br>
 {WORDMARK} {FOOTER_SEP} <a href="{SITE}" style="color:{p['text-dim']}">{SITE}</a><br>
 <a href="{unsub}" style="color:{p['text-dim']}">{UNSUBSCRIBE_LABEL}</a>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


def in_app_welcome(unsubscribe_url: str, email: str = "") -> tuple[str, str, str]:
    """Subject, text part and HTML part for an address registered in-app.

    The one action the mail offers is opening the app the reader just set up,
    and its target is `OPEN_URL` — a fixed page on the site. It takes no URL
    from its caller; `board_url` used to be a parameter here and is gone.
    """
    # WHY THE BUTTON DOES NOT POINT AT THIS INSTALL.
    #
    # It cannot link to `nohuman://` directly -- a mail client will not keep
    # the href of a non-standard scheme. The obvious alternative, and what an
    # earlier revision shipped, was this install's own `<board>/open` route.
    # That route works, but it is served by the very process the button is
    # meant to launch: with no_human closed there is nothing listening (no
    # login item, and `desktop/serverLifecycle.mjs` SIGKILLs the server on
    # quit), so the button was dead in exactly the case it exists for. It was
    # also loopback, so it went nowhere when the mail was read on a phone.
    #
    # A page on the site has neither problem: it does not depend on the
    # reader's machine running anything, and it is reachable from a phone. See
    # `OPEN_URL` for what that page still owes -- it is not deployed yet.
    return (SUBJECT,
            _text_part(unsubscribe_url, email),
            _html_part(unsubscribe_url, email))
