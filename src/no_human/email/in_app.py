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

#: Mirrors config.DEFAULT_CONFIG["server"]["port"]. Used only when the config
#: cannot be read at all -- `local_board_url` prefers the install's real value.
_DEFAULT_PORT = 8420
#: A host the app binds to but a browser cannot open. 0.0.0.0 / :: mean "every
#: interface"; the URL a human clicks has to name a reachable one.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", ""}

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
#: Fallback only. The real URL is this install's own board -- see
#: `local_board_url`, which the renderer calls when no URL is passed.
CTA_URL_FALLBACK = f"http://127.0.0.1:{_DEFAULT_PORT}"
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


def _safe_host(host: str) -> str:
    """A host a browser can open, or loopback. Shared by BOTH entry points.

    This lived only in `local_board_url`, i.e. only on the CONFIG path -- while
    the route PREFERS `app.state.board_url`, which `nh start` builds from a raw
    `--host`. The preferred path skipped every check here. Measured before this
    was shared: `--host 0.0.0.0` (the container image's own documented default,
    docs/security.md) mailed `http://0.0.0.0:8420/open`; `--host ::1` mailed
    `http://::1:8420/open`, the exact spelling this module claims to fix; and
    `--host '['` made `send_welcome` RAISE, which its docstring forbids.
    """
    host = (host or "").strip()
    if host in _WILDCARD_HOSTS:
        return "127.0.0.1"
    if ":" in host:
        import ipaddress
        bare = host.strip("[]")
        try:
            ipaddress.IPv6Address(bare)
        except ValueError:
            return "127.0.0.1"
        # A ZONE id is accepted by ipaddress but needs percent-encoding to be
        # legal in a URL (RFC 6874: `%25en0`), and a link-local address is not
        # reachable from a mail client anyway.
        return "127.0.0.1" if "%" in bare else f"[{bare}]"
    # A host that cannot appear in a URL is a broken config, and the honest
    # answer is the documented default, not a link that cannot resolve.
    if not host or any(c in host for c in ' \t\r\n/?#@\\[]'):
        return "127.0.0.1"
    return host


def _safe_port(port: object) -> int:
    """A port in range, or the documented default."""
    try:
        # bool before int: `port: yes` in YAML parses as True, and int(True)
        # is 1 -- a plausible-looking port nothing is listening on.
        n = _DEFAULT_PORT if isinstance(port, bool) else int(port)
    except (TypeError, ValueError):
        return _DEFAULT_PORT
    return n if 1 <= n <= 65535 else _DEFAULT_PORT


def sanitize_board_url(url: str) -> str:
    """Re-assemble `url` from validated parts, or fall back to the default.

    Returns a REBUILT url rather than the input, so a value that merely parsed
    cannot carry anything through: `http://box:8420\nX-Evil: 1` used to come
    back with the newline intact.
    """
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit((url or "").strip())
        # `.hostname`/`.port` are PARSED values; `.netloc` is not -- a netloc
        # of ":8420" is truthy and names no host at all. `.port` raises on an
        # out-of-range value, and urlsplit itself raises on a bare "[".
        host, port = parts.hostname, parts.port
    except ValueError:
        return CTA_URL_FALLBACK
    if parts.scheme.lower() not in ("http", "https") or not host or parts.username:
        return CTA_URL_FALLBACK
    # Keep the port ONLY if the URL carried one. Defaulting an absent port to
    # the board's 8420 rewrote `https://proxy.example.com/nh` into
    # `https://proxy.example.com:8420/nh` -- a different address, on a URL that
    # was already correct and whose scheme default (443) is what it meant.
    netloc = _safe_host(host) if port is None else f"{_safe_host(host)}:{_safe_port(port)}"
    # Path is KEPT (a board behind a proxy at /nh is legitimate); query and
    # fragment are dropped, as the `/open` join already did.
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def local_board_url(config: dict | None = None) -> str:
    """The URL that opens THIS install's board.

    `notifications.board_url` first: that key already exists for exactly this
    purpose (the Teams notifier's Action.OpenUrl button), and an operator whose
    board is reachable from another device is the one who sets it. Otherwise
    the configured `server.host`/`server.port`, with a wildcard bind rewritten
    to loopback because 0.0.0.0 is not something a browser can open.

    Never raises. This runs on the onboarding path, where a transport problem
    must not break registration, so an unreadable config degrades to the
    documented default rather than propagating.

    KNOWN BOUND, stated rather than hidden: this is a LOOPBACK URL, so it only
    works on the machine running no_human. Read on a phone it goes nowhere.
    The link is still http rather than the `nohuman://` scheme the desktop app
    registers (see desktop/main.mjs) because a mail client will not keep a
    custom-scheme href -- measured against Gmail, which strips it and leaves a
    dead button. The `/open` route this URL points at is what bridges the two.
    """
    try:
        if config is None:
            from ..config import load_config
            # create_if_missing=False: rendering an email must not CREATE
            # ~/.no_human/config.yaml, nor chmod the directory. Measured: the
            # default (True) did exactly that, turning a pure renderer into a
            # filesystem side effect on a path nothing had tested.
            config = load_config(create_if_missing=False)
        notifications = config.get("notifications") or {}
        explicit = notifications.get("board_url")
        if isinstance(explicit, str) and explicit.strip():
            # Parsed, not split on ":". Taking the prefix accepted a BARE
            # scheme -- "http" has no colon at all, so `split(":")[0]` is
            # "http", and the button's href became the relative `http/open`.
            # urlsplit plus a netloc requirement is what actually establishes
            # "this is an absolute http(s) URL". Credentials are refused too:
            # a userinfo in a mailed link is a phishing shape, not a board.
            return sanitize_board_url(explicit)
        server = config.get("server") or {}
        host = str(server.get("host") or "").strip()
        port = server.get("port")
    except Exception:  # noqa: BLE001 - see "never raises" above
        return CTA_URL_FALLBACK
    return f"http://{_safe_host(host)}:{_safe_port(port)}"


def _text_part(unsubscribe_url: str, email: str, board_url: str) -> str:
    """The plain-text part: same copy, wrapped, in base.py's own shape."""
    return f"""{_greet(email)}

{_INTRO}

{textwrap.fill(BODY, 72)}

{CTA_LABEL}: {board_url}

{REPLY_NOTE}

{SIGNOFF}""" + _footer(WHY, unsubscribe_url)


def _html_part(unsubscribe_url: str, email: str, board_url: str) -> str:
    """The HTML part: a single centred card on the board canvas.

    Table-based and fully inlined because that is what survives the clients
    people actually read mail in: Gmail strips <style> blocks, and Outlook's
    Word engine ignores CSS backgrounds on divs (hence `bgcolor` attributes
    alongside every background) and drops border-radius (the pill degrades to
    a rectangle, which is fine — no VML hack).

    The THREE RUNTIME INPUTS are escaped -- the greeting, the unsubscribe URL
    and the board URL -- because those are the only values here that come from
    outside this module. `greeting_name` derives the greeting from the
    address's local part and does NOT sanitise: measured,
    `greeting_name("dana<script>alert(1)</script>@x.io")` returns
    'Dana<script>alert(1)</script>'. Harmless while the body was text; an
    injection the moment it is markup.

    The copy constants and the palette are NOT escaped: they are trusted
    literals in this file. That is a constraint on editing them, so it is
    stated rather than assumed -- a `<`, `&` or quote added to a copy constant
    lands raw in the markup. Keep them free of those three characters, or
    escape at the interpolation site when that stops being practical.
    """
    p = PALETTE
    greet = _html.escape(_greet(email))
    unsub = _html.escape(unsubscribe_url, quote=True)
    cta = _html.escape(board_url, quote=True)

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
   border-radius:999px"><a href="{cta}"
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


def in_app_welcome(unsubscribe_url: str, email: str = "",
                   board_url: str | None = None) -> tuple[str, str, str]:
    """Subject, text part and HTML part for an address registered in-app.

    `board_url` defaults to this install's own board: the one action the mail
    offers is opening the app the reader just set up.
    """
    # `/open` rather than the board root: that route hands off to the
    # `nohuman://` scheme the desktop app registers, so the button opens the
    # APP. It cannot link to the scheme directly — measured, Gmail strips the
    # href of any non-standard scheme, leaving a dead button.
    from urllib.parse import urlsplit, urlunsplit
    base = urlsplit(sanitize_board_url(board_url) if board_url
                    else local_board_url())
    # urlsplit, not concatenation: `http://box:8420/?theme=dark` + "/open"
    # produced a QUERY of `theme=dark/open` and a path of `/`, i.e. the board
    # root, not the handoff. Query and fragment are dropped on purpose.
    path = base.path.rstrip("/")
    if not path.endswith("/open"):
        path += "/open"
    target = urlunsplit((base.scheme, base.netloc, path, "", ""))
    return (SUBJECT,
            _text_part(unsubscribe_url, email, target),
            _html_part(unsubscribe_url, email, target))
