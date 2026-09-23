// Which of the wizard's two navigation controls are live while an awaited call
// is in flight.
//
// The defect this replaces: Back read `disabled={i === 0 || busy}`, and `busy` is
// ONE flag set by Onboarding's `guard()` around every awaited call in the wizard.
// Entering the repos step kicks off a background repository scan through that
// same guard, so Back went dead the moment the step opened, for the whole scan,
// with nothing on screen tying the two together. The first external tester read
// that as "the back button doesn't work".
//
// Nothing is protected by pinning the user in place for that scan: it is a plain
// GET of /api/repos/discover, served by repo_discovery.discover_repos — a
// filesystem walk plus `git status --no-optional-locks --ignore-submodules`, with
// GIT_OPTIONAL_LOCKS=0. It writes nothing, and the step already carries a
// "Re-scan" button because re-running it is the normal thing to do. Leaving it is
// therefore free: the in-flight result still lands in the wizard's state, and the
// step it belongs to picks it up when the user returns.
//
// The one awaited call that genuinely cannot be abandoned is the final launch on
// the last step: it confirms rules, creates projects, writes onboarding config
// and then hands control to the app. That one keeps the lock — and the primary
// button next to it already reads "Launching…", so the lock explains itself
// instead of looking dead.
//
// Both predicates are expressed here rather than inline so they can be tested:
// this project's `node --test` harness has no React renderer, so a disabled rule
// living inside JSX can only ever be checked by grepping for its own source text.

/** True when the forward control (Continue, or the final launch button) must be
 *  locked. Only the terminal launch qualifies. */
export function forwardDisabled({ index, lastIndex, busy }) {
  return Boolean(busy) && index === lastIndex;
}

/** True when Back must be locked: on the first step (there is nowhere to go),
 *  and while the terminal launch is running. Notably NOT during the repos scan
 *  or any other abandonable background call. */
export function backDisabled({ index, lastIndex, busy }) {
  if (index <= 0) return true;
  return forwardDisabled({ index, lastIndex, busy });
}

/** What to tell the user when Back is locked for a reason other than "you are on
 *  the first step" — so a disabled control says why rather than reading as dead.
 *  Empty string when Back is not locked, or locked only because index === 0. */
export function backDisabledReason({ index, lastIndex, busy }) {
  if (index <= 0) return "";
  return forwardDisabled({ index, lastIndex, busy })
    ? "Finishing setup — this last step can't be interrupted."
    : "";
}

// ── clickable step indicator (spec §3 B2) ────────────────────────────────────
// A real user asked to jump between steps instead of clicking Back six times.
// No step gates any other, so any step is reachable from any step — the only
// thing that must not be interrupted is the terminal launch (busy on the last
// step), and `busy` already covers that.

/** True when clicking a step to jump to it is allowed. Every step is reachable;
 *  the one exception is while an awaited call is in flight (`busy`), where a jump
 *  would race the wizard's own state — matching the Back/Continue lock. */
export function canJumpTo({ from, to, busy }) {
  return !busy;
}

/** True when a step's dot may render "done". Position alone used to decide
 *  it (`idx < current`), so a stepper jump from Welcome to Launch painted
 *  Email green while the wizard would refuse to complete for want of an
 *  address — the step described a state the wizard had not actually reached. */
export function stepDone({ key, idx, current, emailSatisfied }) {
  if (idx >= current) return false;
  return key === "email" ? Boolean(emailSatisfied) : true;
}

/** The accessible name for a step button: its title, its 1-based position, and
 *  whether it is where you are, done, or still ahead. `total` is STEPS.length,
 *  passed from the wizard so the label can say "of N". `done`, when passed,
 *  overrides the default `idx < current` completion check (see `stepDone`);
 *  omitting it preserves every existing label byte-for-byte. */
export function stepButtonLabel(step, idx, current, total, done) {
  const isDone = done ?? idx < current;
  const state = idx === current ? "current" : isDone ? "completed" : "not started";
  const of = total ? ` of ${total}` : "";
  return `${step.title}, step ${idx + 1}${of}, ${state}`;
}
