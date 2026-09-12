"""``nh brain sync`` — pull the delta, verify the chain, apply what survives.

**Fail closed on TRUST, open on AVAILABILITY.** The two are different failures
and collapsing them either puts the service on a critical path or lets a forged
rule through.

  availability — timeout, refused connection, 5xx, DNS. The delta is not
                 verifiable YET. Already-verified rules keep working unchanged;
                 new, unverified versions are held aside and applied by nothing.
                 Retry on the next explicit sync. Nothing is discarded.

  trust        — bad signature, digest mismatch, chain break, team_id mismatch,
                 subject mismatch, an unusable algorithm. EVERY remote rule
                 stops being injected, the brain is marked untrusted
                 persistently, the offending envelope is written to
                 ``~/.no_human/brain/quarantine/`` for forensics, and there is
                 NO automatic recovery — only ``nh brain trust --reset``.

Why fail-closed is safe here, and this is the argument the whole design rests
on: **the closed state IS the current product.** Zero remote rules is exactly
how ``nh`` behaves today, so there is no availability cliff to trade against
security and no reason to ever take the insecure branch.

WHAT IS VERIFIED, and where it departs from the design document — stated here
rather than discovered later, because two of the design's seven checks cannot be
performed against the control plane as shipped:

  1. ``sha256(canonical(manifest))`` equals the envelope's ``sha256``.
  2. The RSASSA-PSS SHA-256 signature verifies over that digest against a key
     PINNED IN THIS BUILD.
  3. ``manifest.team_id`` equals this credential's team. THIS IS THE
     ROUTING-LAYER DEFENCE: a misrouted response would still carry a valid
     signature, and only the team id catches it. (A vendor once served one
     shared database to every tenant for 34 days.)
  4. ``manifest.version`` is the version that was asked for.
  5. ``manifest.prev_sha256`` equals the head of the chain already held. The
     server advances that head with a conditional write, so a fork is a break.
  6. ``manifest.subject`` equals, field for field, the row the delta returned at
     that version. A mismatch means the table and the signed record disagree —
     the exact tamper case the chain exists to catch.
  7. NOT IMPLEMENTED AS DESIGNED — "distinct actors in ``approvals`` >= the
     rule's ``required_approvals``". Neither operand exists on the wire:
     ``required_approvals`` lives on the PROPOSAL item and is absent from the
     signed rule ``subject``, and the manifest's ``approvals`` list is built
     with only the FINAL approver, not all of them. So the signed document
     cannot answer "did two people approve this". What is enforced instead is
     the weakest true statement — at least one named approver is recorded — and
     the count is rendered into the prompt as provenance. Closing this properly
     is a control-plane change (put ``required_approvals`` and every voter in
     the manifest), not a client one.

A version that has NO manifest is normal and is NOT a trust failure: the control
plane allocates a version to filed proposals and to the re-stamped old row of a
supersede, and writes exactly one manifest per admission. Such a row is stored
inert with reason ``unverified-no-manifest`` and is never injected — fail-closed
for the row, without quarantining the team. The design document lists a missing
manifest as a trust failure; implementing that literally would turn every
ordinary supersede into a false alarm.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

from . import client as http
from . import credentials as creds
from . import screen as screens
from . import store as brainstore
from . import verify
from .keys import PINNED_SIGNING_KEYS
from .settings import MAX_BRAIN_SCHEMA, BrainConfig

#: Bound the paging loop. The server caps a page at 200 items; a team that
#: cannot be caught up in this many pages is a bug, not a big team.
_MAX_PAGES = 200


class TrustFailure(RuntimeError):
    """A signature, chain or identity check failed. Quarantines everything."""


@dataclass
class SyncReport:
    pages: int = 0
    items: int = 0
    manifests_verified: int = 0
    applied: int = 0
    refused: list = field(default_factory=list)   # (rule_id, reason)
    #: Rows the client declined to even STORE because the manifest covering
    #: them could not be fetched. Distinct from `refused`, which is a verdict.
    held_aside: int = 0
    watermark: int = 0
    complete: bool = False
    note: str = ""


def _quarantine(name: str, payload) -> str:
    directory = creds.quarantine_dir()
    from ..config import ensure_private_dir
    ensure_private_dir(directory)
    path = directory / f"{int(time.time())}-{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def verify_envelope(envelope: dict, *, version: int, team_id: str,
                    chain_head: str | None) -> tuple[dict, str]:
    """Verify one manifest envelope. Returns (manifest, sha256_hex).

    Raises TrustFailure with a specific reason. Every branch here is a
    quarantine-the-whole-brain event; nothing in this function is advisory.
    """
    if not isinstance(envelope, dict):
        raise TrustFailure("manifest envelope is not an object")

    manifest = envelope.get("manifest")
    if not isinstance(manifest, dict):
        raise TrustFailure("manifest envelope carries no manifest")

    algorithm = str(envelope.get("signing_algorithm") or "")
    if algorithm != "RSASSA_PSS_SHA_256":
        raise TrustFailure(f"unsupported signing algorithm {algorithm!r}")

    body = verify.canonical(manifest)
    digest = hashlib.sha256(body).digest()
    if digest.hex() != str(envelope.get("sha256") or ""):
        raise TrustFailure("manifest digest does not match the envelope")

    import base64
    try:
        signature = base64.b64decode(str(envelope.get("signature") or ""), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise TrustFailure("signature is not valid base64") from exc

    if not verify.verify_with_pinned_keys(PINNED_SIGNING_KEYS, digest, signature):
        raise TrustFailure("signature does not verify against any pinned key")

    if str(manifest.get("schema") or "") != "no_human.brain.manifest/1":
        raise TrustFailure(f"unknown manifest schema {manifest.get('schema')!r}")

    if str(manifest.get("team_id") or "") != team_id:
        raise TrustFailure("manifest is for a different team")

    if int(manifest.get("version") or -1) != int(version):
        raise TrustFailure("manifest version does not match the version requested")

    prev = manifest.get("prev_sha256")
    prev = None if prev in (None, "") else str(prev)
    if prev != chain_head:
        raise TrustFailure("manifest chain is broken")

    approvals = manifest.get("approvals")
    if not isinstance(approvals, list) or not any(
            isinstance(a, dict) and a.get("actor") for a in approvals):
        raise TrustFailure("manifest records no approver")

    return manifest, digest.hex()


def _subjects_match(subject, delta_row) -> bool:
    """Field-for-field equality between the SIGNED item and the served row.

    Compared on the intersection of keys the wire projection emits, with numbers
    normalised: the delta and the manifest are produced by the same allowlist,
    but a row that has since been re-stamped legitimately carries a different
    ``version`` and ``status``. Only rows AT the manifest's own version are ever
    compared, so any difference here is a disagreement between the table and the
    signed record.
    """
    if not isinstance(subject, dict) or not isinstance(delta_row, dict):
        return False

    def normal(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, list):
            return [normal(v) for v in value]
        if isinstance(value, dict):
            return {k: normal(v) for k, v in sorted(value.items())}
        return value

    for key in ("rule_id", "scope", "type", "title", "content", "visibility",
                "status", "version", "tags"):
        if key in subject or key in delta_row:
            if normal(subject.get(key)) != normal(delta_row.get(key)):
                return False
    return True


def run(cfg: BrainConfig, id_token: str, team_id: str, db_path,
        *, transport=None) -> SyncReport:
    """One explicit sync. Foreground, no thread, no retry storm."""
    conn = brainstore.connect(db_path)
    report = SyncReport()
    try:
        if brainstore.trust(conn) == brainstore.TRUST_UNTRUSTED:
            raise TrustFailure(
                "this brain is marked untrusted "
                f"({brainstore.get_state(conn, 'trust_reason', 'unknown')}). "
                "Nothing will sync or inject until `nh brain trust --reset`.")

        since = brainstore.watermark(conn)
        report.watermark = since

        for _page in range(_MAX_PAGES):
            try:
                delta = http.sync_delta(cfg, id_token, since, transport=transport)
            except http.BrainHTTPError as exc:
                if exc.status == 409:
                    # The server cannot replay deletions in this window. The
                    # known failure is "you may still be applying a rule the
                    # team withdrew", so this degrades to FEWER rules, not
                    # staler ones: drop everything and resync from zero.
                    #
                    # That sentence is only true because of `keep_high_water`
                    # and the check after this loop. A reset clears the
                    # watermark AND `chain_head_sha256`, which is exactly the
                    # state in which a control plane can answer the refetch with
                    # a genuinely-signed but OLDER chain prefix: every signature
                    # verifies, every link chains, and a withdrawn rule comes
                    # back. The high-water mark is what the replayed prefix
                    # cannot climb over.
                    brainstore.reset(conn, keep_high_water=True)
                    since = 0
                    report.note = ("server required a full resync; local state "
                                   "was discarded and refetched from version 0")
                    continue
                raise

            schema = int(delta.get("schema_version") or 1)
            if schema > MAX_BRAIN_SCHEMA:
                report.note = (
                    f"the control plane is on brain schema {schema}; this build "
                    f"understands {MAX_BRAIN_SCHEMA}. Syncing stopped and the "
                    f"verified cache was kept — upgrade no_human.")
                break

            report.pages += 1
            items = [i for i in (delta.get("items") or []) if isinstance(i, dict)]
            report.items += len(items)

            stopped_at, top_verified = _apply_page(
                conn, cfg, id_token, team_id, items, report,
                transport=transport)

            watermark = int(delta.get("watermark") or since)
            if stopped_at is not None:
                # Do not step over the gap. Advancing to `stopped_at - 1` rather
                # than leaving `since` alone is deliberate and load-bearing: the
                # versions BELOW the gap were verified and the chain head has
                # already advanced past them, so refetching them next time would
                # re-present an already-chained manifest and read as a break.
                watermark = min(watermark, stopped_at - 1)
            if top_verified:
                # ENFORCE, rather than assume, that the reported watermark
                # covers the page it came with. An earlier comment here asserted
                # "in the healthy case `since >= high_water` always holds" as if
                # it were part of the wire contract; nothing in this client made
                # it true, and a server whose `watermark` trailed the items in
                # its OWN page falsified it with no malice at all — an
                # availability bug, not an attack.
                #
                # Leaving the watermark short of a version this page VERIFIED is
                # what makes the next sync refetch an already-chained manifest,
                # whose `prev_sha256` then points behind the head and reads as a
                # fork: an unrecoverable trust failure caused by a lagging
                # server. `top_verified` is the top version whose manifest
                # verified in THIS page, so it is below any gap by construction
                # and this can never step over one.
                watermark = max(watermark, top_verified)
            if watermark > since:
                since = watermark
                brainstore.set_state(conn, "watermark", since)
                report.watermark = since
            if stopped_at is not None:
                # Nothing beyond the gap can be verified in this run, so there
                # is no page after this one worth asking for.
                break
            if delta.get("complete", True):
                report.complete = True
                break

        high_water = brainstore.chain_high_water(conn)
        chain_top = brainstore.chain_top(conn)
        if chain_top < high_water:
            # The chain this machine now HOLDS stops before versions it has
            # already verified. That is a replayed prefix, not a recovery, and
            # the only honest degradation is to nothing at all: fewer rules,
            # never staler ones.
            #
            # BOTH OPERANDS ARE SIGNED, and that is the whole of the fix a
            # re-review forced. This read `since < high_water`, and `since` is
            # the `watermark` field of the sync response — plain JSON, in no
            # signed manifest, written entirely by the control plane, which
            # `store._HIGH_WATER` names as the adversary this guard defends
            # against. The adversary wrote one of the two operands, so it chose
            # the comparison's answer: serve the genuinely-signed v2 prefix,
            # report watermark=4, and a rule from below the high-water mark
            # reaches the coder prompt with nothing forged anywhere. Same
            # bypass with no 409 in it, by reporting any number at all after a
            # watermark rewind. `chain_top` is the version of the manifest this
            # machine last verified a SIGNATURE over, so a server that serves
            # less than it served before cannot report its way past this.
            #
            # UNCONDITIONAL — deliberately not `if resynced and ...`, which is
            # what this shipped as and what an earlier re-review broke in three
            # syncs. The guard fired on the 409, cleared the watermark, and the
            # NEXT ORDINARY sync started from 0 with `resynced` False, refetched
            # the same replayed prefix, chained it cleanly against the head the
            # reset had wiped, and put the withdrawn rule back in the coder
            # prompt. Gating a safety property on HOW the sync started makes it
            # a property of the trigger rather than of the chain. The invariant
            # is about the chain: once a chain has been verified to version N,
            # no later sync may inject a rule from a prefix below N.
            #
            # It fires on exactly one thing because `chain_top` and
            # `high_water` rise together on the same statement — a manifest that
            # verified inside a page — so only losing ground can separate them.
            # A LAGGING SERVER NO LONGER TRIPS IT: the previous phrasing did,
            # because its operand could trail the very page it arrived with, and
            # that quarantined a brand-new machine permanently.
            #
            # Not a TrustFailure: every signature in a replayed prefix is
            # genuine, and a permanent quarantine on what may be a rebuilt
            # server is the availability cliff this design exists to avoid. It
            # clears itself the moment the server serves the chain it already
            # served once, and `nh brain trust --reset` — the one reset that
            # forgets the high-water mark — is the exit for a team that
            # legitimately rebuilt its brain.
            #
            # THE RESET TAKES THE HEAD AND THE MANIFESTS WITH THE RULES, and
            # under this formulation of the guard that is load-bearing rather
            # than tidy. Measured, on the fixed code, by replacing exactly this
            # call with "delete the rules, keep the watermark and the head" and
            # running healthy -> replay -> server whole again:
            #
            #   reset(keep_high_water=True)   replay: top=2 inj=[]  applied=0
            #                                 recovery: top=4 inj=[both] applied=2
            #   delete rules only             replay: top=2 inj=[]  applied=0
            #                                 recovery: top=2 inj=[]  applied=0
            #
            # The alternative never recovers. Keeping the watermark at 4 makes
            # the next sync ask for versions ABOVE 4, so a server that is whole
            # again serves nothing, `chain_top` stays pinned at the replayed
            # version, and the guard fires forever. Clearing the watermark is
            # what makes the machine refetch from zero and climb back over the
            # mark; `keep_high_water=True` is what keeps that climb measured
            # against everything this machine has ever verified.
            #
            # Stated this way on purpose: an earlier version of this comment
            # justified the reset by a claim about lagging servers that a
            # reviewer implemented and disproved. The lines above are an
            # experiment's output, and the two tests named for it are
            # `..._clears_itself_when_the_server_catches_up` and
            # `..._documented_escape_hatch_actually_lets_a_machine_back_in`.
            brainstore.reset(conn, keep_high_water=True)
            report.applied = 0
            report.note = (
                f"the chain this machine now holds reaches version {chain_top}, "
                f"before version {high_water} which it had already verified. "
                "The server replayed an older chain; no remote rule will be "
                "injected until a sync catches up. If this team REBUILT its "
                "brain, a sync never will catch up and this is permanent — "
                "`nh brain trust --reset` is the exit (it forgets the "
                "high-water mark and re-verifies from version 0). "
                "`nh brain status` shows this state.")
            return report

        brainstore.set_state(conn, "last_verified_sync", time.time())
        brainstore.set_state(conn, "team_id", team_id)
        return report

    except TrustFailure as exc:
        path = _quarantine("trust-failure", {
            "reason": str(exc),
            "team_id": team_id,
            "watermark": brainstore.watermark(conn),
            "chain_head": brainstore.get_state(conn, "chain_head_sha256"),
        })
        brainstore.mark_untrusted(conn, str(exc))
        raise TrustFailure(f"{exc} (quarantined to {path})") from exc
    finally:
        conn.close()


def _apply_page(conn, cfg, id_token, team_id, items, report, *, transport=None):
    """Walk the page's versions in order: verify the manifest, apply what it
    SIGNED, then advance the chain over it — in that order, once per version.

    ONE LOOP, and that is load-bearing rather than tidy. What this replaced was
    two: a verify loop over every version in the page, and an apply loop over
    the subset of SERVED ROWS the control plane had labelled ``entity ==
    "rule"`` with a non-empty ``rule_id``. Both of those fields are written by
    the server, so the server chose which of the versions it had just been
    proved to have signed would actually be applied — and the verify loop had
    already advanced the head, the high-water mark and the watermark over all of
    them. The dropped admission was therefore permanent: no honest sync refetches
    a version below the watermark. Fused into one loop, "advance" is the last
    statement of an iteration that has already reached a verdict, so there is no
    window in which a version is chained-over but unapplied.

    Returns ``(stopped_at, top_verified)``. ``stopped_at`` is the version at
    which verification STOPPED because a manifest could not be FETCHED, or None
    when the whole page was walked. ``top_verified`` is the highest version
    whose manifest VERIFIED in this page, or 0 if none did — the caller uses it
    to keep the local watermark from being left behind the chain this page
    actually proved, which a server reporting a trailing watermark would
    otherwise do. It is below ``stopped_at`` by construction, so a caller
    clamping to the gap and a caller raising to this cannot contradict.

    WHY A STOP AND NOT A SKIP, which is what shipped. The chain is ordered: each
    manifest's ``prev_sha256`` is the digest of the one admitted before it. Skip
    a version because its manifest 5xx'd and the head does not advance — so the
    NEXT version in the same page compares its ``prev_sha256`` against a head
    that is one link short, reads as a fork, and quarantines the entire brain
    permanently. One transient 503 became an unrecoverable trust failure, which
    is the exact inversion of this module's own contract. Demonstrated on a page
    carrying v2 and v4 with a single HTTP 503 on v2's manifest.

    Stopping at the gap is the only answer that keeps both halves true: nothing
    beyond it is verified (so nothing beyond it is injected), and nothing beyond
    it is CLAIMED to be verified either. The caller holds the watermark back to
    just below the gap, which is what makes "retried on the next sync" a fact
    rather than a note.
    """
    # The served rows, grouped by version. ADVISORY ONLY, and that is the whole
    # of the second fix a re-review forced. EVERY field on one of these rows is
    # written by the control plane, so no field on one may decide what gets
    # applied. This shipped as `{version: row for row in items if
    # row["entity"] == "rule"}` feeding an apply loop keyed on the SERVED rows,
    # with a second `if not rule_id: continue` inside it — two server-authored
    # fields, each of which silently removed a row from the apply set while the
    # manifest covering it still verified and still advanced the chain. Setting
    # `entity` to anything but "rule", or blanking `rule_id`, made the client
    # advance its verified chain to the true head and discard what that head had
    # signed: no 409, no prior state, permanent, on a brand-new machine's first
    # sync, with nothing forged anywhere.
    #
    # ALL rows at a version are kept, not just the first or the last. A page
    # carrying two rows at the same version is the same trick one level down —
    # serve one row that matches the signed subject and one that does not, and a
    # loop that inspects either one alone checks the wrong one half the time.
    # `_apply_subject` compares the signed subject against every row served at
    # its version, so a disagreement anywhere is the trust failure it always was.
    served: dict[int, list] = {}
    for item in items:
        version = int(item.get("version") or 0)
        if version > 0:
            served.setdefault(version, []).append(item)

    stopped_at = None
    top_verified = 0
    for version in sorted(served):
        try:
            envelope = http.manifest(cfg, id_token, version, transport=transport)
        except http.BrainHTTPError:
            # AVAILABILITY, not trust. Stop here: see the docstring.
            stopped_at = version
            report.note = report.note or (
                f"the manifest for version {version} could not be fetched; that "
                f"version and everything after it were held aside and will be "
                f"retried on the next `nh brain sync`")
            break
        if envelope is None:
            # A 404 is different in kind from a 5xx and must NOT stop the page.
            # The server writes one manifest per ADMISSION and chains them by
            # admission, so a version with no manifest is a gap in the VERSION
            # sequence and not a gap in the CHAIN — the head is already correct
            # for whatever is admitted next. Proposals and the re-stamped old
            # row of a supersede are both this case, and both are ordinary.
            #
            # Nothing was signed at this version, so there is no subject to
            # apply and the chain does not move. The rows are stored inert with
            # the reason, which is what `nh brain list` shows.
            for row in served[version]:
                _store_refused(conn, row, "unverified-no-manifest", report)
            continue

        head = brainstore.get_state(conn, "chain_head_sha256")
        manifest, sha = verify_envelope(envelope, version=version,
                                        team_id=team_id, chain_head=head)

        # THE SIGNED SUBJECT IS APPLIED BEFORE THE CHAIN ADVANCES OVER IT, and
        # the ORDER of these two statements is the invariant, not a detail. The
        # defect this replaced had verification and application in two separate
        # loops: the first walked every version and advanced the head and the
        # high-water mark, the second walked a server-filtered subset and
        # applied what survived it. Anything the second loop dropped, the first
        # had already stepped over — and because the watermark had advanced, no
        # honest sync would ever refetch it. Applying first makes "advance" the
        # last statement of an iteration that necessarily reached a verdict, so
        # a version cannot be chained-over without having been applied, refused
        # for a named reason, or raised as a trust failure. There is no third
        # outcome and no `continue` between here and the advance.
        _apply_subject(conn, manifest, version, served[version], report)

        brainstore.record_manifest(conn, version, sha,
                                   manifest.get("prev_sha256"), envelope)
        brainstore.set_state(conn, "chain_head_sha256", sha)
        brainstore.note_verified_version(conn, version)
        report.manifests_verified += 1
        top_verified = version

    if stopped_at is not None:
        # HELD ASIDE, and that means not stored at all — not stored inert with a
        # reason that would read as "the server has no manifest for this", which
        # is a different and terminal thing. Counted over every row at or past
        # the gap, because whether one of them is a rule is a question only its
        # signed manifest can answer and that manifest is the thing we could not
        # fetch.
        report.held_aside += sum(len(rows) for v, rows in served.items()
                                 if v >= stopped_at)

    return stopped_at, top_verified


def _apply_subject(conn, manifest, version, rows, report) -> None:
    """Apply what the manifest SIGNED, cross-checked against what was served.

    The subject is the record. It arrived inside a document whose signature
    verified against a key pinned in this build, so every field on it —
    ``type``, ``scope``, ``status``, ``visibility``, ``rule_id``, ``version``,
    ``title``, ``content`` — is a signed quantity, and every screen below runs
    over signed quantities. The served row is compared against it and then
    discarded. Previously the served row was what got screened and stored, so
    the control plane chose the input to its own admission check.

    Exactly one of three things happens to every version that reaches here, and
    the caller advances the chain only after it has: applied, refused with a
    named reason, or TrustFailure.
    """
    subject = manifest.get("subject")
    if not isinstance(subject, dict):
        # A manifest that verified but signs nothing. The chain cannot advance
        # over a version whose admission this build cannot read.
        raise TrustFailure(
            f"the signed manifest for version {version} carries no subject")

    # Check #6, unchanged in meaning and now applied to every row served at this
    # version rather than to one of them.
    for row in rows or ():
        if not _subjects_match(subject, row):
            raise TrustFailure(
                f"the signed manifest for version {version} does not match the "
                f"row the server returned")

    approvals = manifest.get("approvals")
    verdict = screens.screen(subject)
    if not verdict.ok:
        _store_refused(conn, subject, verdict.reason or "refused", report,
                       manifest_version=version, approvals=approvals)
        return

    brainstore.upsert_rule(
        conn, subject, injectable=True, refused_reason=None,
        manifest_version=version, approvals=approvals)
    report.applied += 1


def _store_refused(conn, row, reason, report, *, manifest_version=None,
                   approvals=None):
    # `rule_id` is the table's PRIMARY KEY, so a row without one cannot be
    # stored without colliding with every other row without one. It is still
    # REPORTED — the reason is the thing an operator reads, and dropping the
    # report entry too would restore in miniature the silence this whole change
    # exists to remove.
    rule_id = str(row.get("rule_id") or "")
    if rule_id:
        brainstore.upsert_rule(conn, row, injectable=False,
                               refused_reason=reason,
                               manifest_version=manifest_version,
                               approvals=approvals)
    report.refused.append((rule_id or "?", reason))
