// The ONE place the app's own source carries the community invite. It is also
// printed 13 times across README.md, README.ja.md, README.ko.md, README.zh-CN.md
// and .github/ISSUE_TEMPLATE/config.yml — documentation copies for readers who
// never launch the app, deliberately NOT edited by this change. Anything the
// board renders imports it from here; a second literal in a component is the
// defect onboardingDiscord.test.mjs exists to catch.
export const DISCORD_INVITE_URL = "https://discord.gg/mSARvj6yW6";
