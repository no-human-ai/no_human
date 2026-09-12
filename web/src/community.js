// The ONE place the app's own source carries the community invite. It is also
// printed in every README translation and in the issue-template config —
// documentation copies for readers who never launch the app. Deliberately NO
// count and NO file list here: this comment used to carry both, and by the
// time the test stopped hand-listing those files this comment was already
// wrong about them (13 across 5 files, when the tree had 25 across 9). A
// hand-list in a comment goes stale exactly as fast as one in a test, and
// nothing checks a comment. onboardingDiscord.test.mjs DISCOVERS the carriers
// on every run and asserts each equals this constant. Anything the board
// renders imports it from here; a second literal in a component is the defect
// that test exists to catch.
export const DISCORD_INVITE_URL = "https://discord.gg/mSARvj6yW6";
