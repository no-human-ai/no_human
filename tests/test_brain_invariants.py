"""The five invariants that let a hosted-tier client live in the LOCAL product.

The project's lean-stack rule permits the hosted AWS tier to use whatever it
needs, and then binds the half that governs this repo: *nothing the hosted tier adds may
become a dependency of the local product*. The team-brain client is allowed to
exist here only under five falsifiable statements, and a promise in a design
document is not a boundary — the whole reason the cloud lives in a separate repo
is that prose was not trusted to keep the stacks apart.

So each invariant gets a test that FAILS if it is violated:

  L1  zero new runtime packages
  L2  zero new stores, processes or services
  L3  no AWS surface reaches the developer's machine
  L4  inert by default, and removable
  L5  the service is never on a critical path

plus the auth separation invariants (A1-A5), which are what make the brain
credential structurally different from the Claude credential rather than merely
differently filed.

Every test here is written to have a KNOWN POSITIVE it could detect. A guard
that cannot be shown to fire is indistinguishable from no guard, so where a test
asserts an absence it is paired with a mutation that proves the probe works.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "no_human"
BRAIN = SRC / "brain"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _brain_sources(root: Path | None = None) -> list[Path]:
    """Every ``.py`` under the package, at ANY depth.

    ``rglob``, not ``glob``. This shipped as ``glob("*.py")``, which scans one
    directory — so L1 (no new dependency), L2 (no thread or process), A1 (never
    writes os.environ) and A5 (never disables TLS verification) were ALL blind
    to any subpackage. A single file in ``brain/bg/`` evaded four invariants at
    once, and none of them failed. A gate is exactly as wide as the file list it
    is handed, and that list is the thing to test.
    """
    return sorted((root or BRAIN).rglob("*.py"))


def _code_text(path: Path) -> str:
    """The file with COMMENTS AND STRING LITERALS REMOVED.

    Every structural gate below greps this rather than the raw file, and the
    reason is not convenience. These modules document what they must not do —
    "no ``verify=False``", "not ``boto3``", "never a thread" — and a gate that
    matched prose would fire on its own explanation, which is the fastest way to
    get a gate deleted. Grepping code only means the gate says exactly what it
    means: this behaviour is not PRESENT, whatever the prose says about it.

    Deployment IDENTIFIERS (an ARN, a region, a hostname) are held to the
    stricter standard and are checked against the whole file, prose included —
    see ``_AWS_IDENTIFIERS``.
    """
    import io
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    del io
    return "\n".join(kept)


# --------------------------------------------------------------------------- #
# L1 — zero new runtime packages                                              #
# --------------------------------------------------------------------------- #

# Frozen literal, on purpose. `httpx` is what the client is written against and
# it was ALREADY here; everything else on this list predates the client. If the
# brain (or anything else) ever needs a package, this test is the thing that
# makes that a decision somebody takes rather than a line somebody adds.
FROZEN_DEPENDENCIES = [
    "aiosqlite>=0.22.1",
    "claude-agent-sdk>=0.2.120",
    "click>=8.4.1",
    "fastapi>=0.138.0",
    "httpx>=0.28.1",
    # Raised to mcp>=2 after porting intake/mcp_bridge.py to mcp.server.mcpserver.
    "mcp>=2",
    "psutil>=7.0.0",
    "pyyaml>=6.0.3",
    "rich>=15.0.0",
    "slack-sdk>=3.43.0",
    "textual>=8.2.7",
    "uvicorn[standard]>=0.49.0",
]


def _declared_dependencies() -> list[str]:
    # `tomllib` is stdlib from 3.11 and this project declares
    # `requires-python = ">=3.12"`, so the ModuleNotFoundError fallback that
    # stood here was unreachable on every supported interpreter. It is removed
    # rather than kept for safety: what it did was `pytest.skip`, so on the one
    # interpreter where it COULD have fired it would have silently disabled
    # L1 — the frozen dependency list, which is the invariant that keeps the
    # hosted tier from adding a package to the local product. A guard whose
    # failure mode is "skip the guard" is worse than no guard, and the repo's
    # own tamper detector counts a skip marker as a cheat signal for exactly
    # that reason.
    import tomllib

    with PYPROJECT.open("rb") as handle:
        return list(tomllib.load(handle)["project"]["dependencies"])


def test_L1_the_dependency_list_is_unchanged():
    assert _declared_dependencies() == FROZEN_DEPENDENCIES


def test_L1_no_new_runtime_package_by_name():
    """The half of L1 that a version-bound edit must never be able to satisfy.

    The exact-string comparison above is deliberately brittle so that even a
    loosened bound is a decision somebody takes. This one states the property
    that must hold no matter how the bounds move: the SET OF PACKAGES is
    closed. A future contributor editing the frozen literal for a bound has to
    leave this one alone; adding a package trips both.
    """
    def _names(reqs):
        return sorted(r.split(">=")[0].split("==")[0].split("[")[0].split("<")[0].strip()
                      for r in reqs)

    assert _names(_declared_dependencies()) == _names(FROZEN_DEPENDENCIES)
    assert _names(_declared_dependencies()) == [
        "aiosqlite", "claude-agent-sdk", "click", "fastapi", "httpx", "mcp",
        "psutil", "pyyaml", "rich", "slack-sdk", "textual", "uvicorn",
    ]


def test_L1_probe_detects_an_added_dependency():
    """The known positive: prove the comparison above can actually fail."""
    assert _declared_dependencies() != FROZEN_DEPENDENCIES + ["boto3>=1.0"]


# Import name -> the distribution it comes from, for the frozen list above.
_ALREADY_DECLARED = {
    "httpx": "httpx", "click": "click", "rich": "rich", "yaml": "pyyaml",
    "fastapi": "fastapi", "aiosqlite": "aiosqlite", "psutil": "psutil",
    "mcp": "mcp", "textual": "textual", "uvicorn": "uvicorn",
    "slack_sdk": "slack-sdk", "claude_agent_sdk": "claude-agent-sdk",
}


def test_L1_every_third_party_import_was_already_a_dependency():
    """No import in src/no_human/brain/ names a package the product lacked.

    `cryptography` is the one this is really guarding: RSASSA-PSS verification
    over a 2048-bit key is public-data-only integer arithmetic, so the verifier
    is stdlib, and adding a compiled crypto dependency to a laptop install
    because the hosted tier signs things is exactly the bleed the lean-stack rule
    forbids.
    """
    declared = {d.split(">")[0].split("=")[0].split("[")[0]
                for d in _declared_dependencies()}
    stdlib = set(sys.stdlib_module_names)
    for path in _brain_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in stdlib:
                    continue
                assert _ALREADY_DECLARED.get(name) in declared, (
                    f"{path.name} imports {name!r}, which is not an "
                    f"already-declared dependency")


def test_L1_no_cryptography_anywhere_in_the_client():
    joined = "\n".join(_code_text(p) for p in _brain_sources())
    for banned in ("cryptography", "OpenSSL", "nacl", "pycryptodome", "Crypto"):
        assert banned not in joined


# --------------------------------------------------------------------------- #
# L2 — zero new stores, processes or services                                 #
# --------------------------------------------------------------------------- #

# Every way this codebase starts something that outlives the call. A daemon, a
# scheduler, a background watcher and a queue are all the same violation, so
# they are all one list — and it is matched against the AST, not the text, so
# neither a docstring that mentions a thread nor an alias that hides one changes
# the answer.
_FORBIDDEN_MODULES = frozenset({
    "threading", "multiprocessing", "subprocess", "sched", "concurrent",
    "_thread", "signal", "asyncio",
    # A dynamic import this scan could not read. See _UNANALYSABLE.
    "<computed-at-runtime>",
})
_FORBIDDEN_CALLS = frozenset({
    "threading.Thread", "multiprocessing.Process", "subprocess.Popen",
    "subprocess.run", "subprocess.check_output", "os.fork", "os.spawnv",
    "os.system", "asyncio.create_task", "asyncio.ensure_future",
    "loop.run_in_executor",
})


#: The ways a module is named that are NOT an `import` statement. The AST scan
#: below saw `import threading` and missed `__import__("threading")` and
#: `importlib.import_module("subprocess")`, both of which reach exactly the
#: modules L2 forbids while reading as an ordinary call.
_DYNAMIC_IMPORTERS = frozenset({
    "__import__", "import_module", "importlib.import_module",
    "importlib.__import__", "builtins.__import__",
})

#: What an unanalysable dynamic import resolves to. A name built at runtime
#: cannot be screened, so it is treated as the violation it might be rather than
#: as the absence of one — the scan must never answer "clear" to a question it
#: could not read.
_UNANALYSABLE = "<computed-at-runtime>"


def _module_names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and \
                ast.unparse(node.func) in _DYNAMIC_IMPORTERS:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value.split(".")[0])
            else:
                found.add(_UNANALYSABLE)
    return found


def _call_names(tree: ast.AST) -> set[str]:
    return {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}


def _assert_starts_nothing(paths) -> None:
    """The L2 gate itself, over an arbitrary file list.

    Factored out so the known-positive below can run the REAL gate over a REAL
    tree without that tree being the shared source directory — see
    ``test_the_structural_gates_see_a_violation_in_a_real_subpackage``.
    """
    for path in paths:
        tree = ast.parse(path.read_text())
        offending = _module_names(tree) & _FORBIDDEN_MODULES
        assert not offending, f"{path.name} imports {sorted(offending)}"
        calls = _call_names(tree) & _FORBIDDEN_CALLS
        assert not calls, f"{path.name} calls {sorted(calls)}"


def test_L2_the_client_starts_no_thread_process_or_scheduler():
    _assert_starts_nothing(_brain_sources())


def test_L2_probe_detects_a_spawned_thread():
    """Known positive for the scan above: prove it can actually fail."""
    tree = ast.parse("import threading\nthreading.Thread(target=f).start()\n")
    assert _module_names(tree) & _FORBIDDEN_MODULES
    assert _call_names(tree) & _FORBIDDEN_CALLS


def test_L2_synced_state_lands_in_the_products_own_database(tmp_path):
    """No second datastore. The brain tables are created inside the product's
    existing SQLite file, by this package, at runtime — `migrations/` is not
    where they belong, because a new .sql file would never run for a user who
    installed a wheel built before it."""
    import sqlite3

    from no_human.brain import store

    db = tmp_path / "no_human.db"
    conn = store.connect(db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert {"brain_rules", "brain_manifests", "brain_state",
            "brain_blocks"} <= names
    # And nothing else was created beside it.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["no_human.db"]
    sqlite3.connect(db).close()


def test_L2_the_only_file_the_client_creates_is_its_credential(tmp_path, monkeypatch):
    """The set of files under ~/.no_human/ the client may create is exactly
    {brain/credentials.json, brain/quarantine/*}."""
    from no_human.brain import credentials

    monkeypatch.setenv("NO_HUMAN_HOME_OVERRIDE", str(tmp_path / "home"))
    cred = credentials.Credential(refresh_token="r", id_token="i",
                                  id_token_expires_at=0.0, team_id="t",
                                  subject="s")
    credentials.save(cred)
    created = sorted(
        str(p.relative_to(tmp_path / "home"))
        for p in (tmp_path / "home").rglob("*") if p.is_file())
    assert created == ["brain/credentials.json"]


def test_L2_the_credential_file_and_directory_are_private(tmp_path, monkeypatch):
    from no_human.brain import credentials

    monkeypatch.setenv("NO_HUMAN_HOME_OVERRIDE", str(tmp_path / "home"))
    path = credentials.save(credentials.Credential("r", "i", 0.0, "t", "s"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.parent.stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------- #
# L3 — no AWS surface reaches the developer's machine                         #
# --------------------------------------------------------------------------- #

# Shaped like the repo's existing packaging carve-outs: a grep gate that fails
# the build. The hosted tier is built out of DynamoDB, Lambda, KMS, S3, Cognito,
# IAM session tags and Terraform. The local product knows ONE base URL, and the
# service's shape is deliberately unlearnable from the client.
#
# TWO gates at two strictnesses, because they guard two different things.

# (a) DEPLOYMENT IDENTIFIERS — an ARN, a region, a service hostname, a bucket
#     URI, an access-key id. These name somebody's actual cloud, and they are
#     banned from the WHOLE FILE, prose included: a comment is not code, but a
#     comment carrying a real ARN still teaches a reader the topology this
#     client is built not to know.
_AWS_IDENTIFIERS = re.compile(
    r"""(?ix)
    arn:aws | \bamazonaws\.com | \bamazoncognito\.com |
    \b(?:us|eu|ap|sa|ca|me|af)-(?:east|west|north|south|central|northeast|
        southeast|northwest|southwest)-\d\b |
    \bexecute-api\b | \blambda-url\b | s3:// | \bAKIA[0-9A-Z]{16}\b
    """
)

# (b) SDK AND SERVICE VOCABULARY — the SDK, a signing scheme, a store, a role
#     assumption. Banned from CODE, checked against the token stream with
#     comments and string literals removed. Allowed in prose precisely so this
#     package can state at the top of every module which of these it refuses to
#     be: a gate that fires on its own rationale is a gate somebody deletes.
_AWS_VOCABULARY = re.compile(
    r"""(?ix)
    \bboto3\b | \bbotocore\b | \bdynamodb\b | \bsigv4\b |
    \bAWS_ACCESS_KEY | \bAWS_SECRET_ACCESS_KEY | \bAWS_SESSION_TOKEN |
    \bassume_?role\b | \bLeadingKeys\b
    """
)

# Whole-product scan, not just the brain package: the invariant is about the
# LOCAL PRODUCT, and a leak in api/ or cli/ would be just as much of one.
_SCAN_ROOTS = ("brain", "core", "cli", "api", "config.py")


def _scan_targets() -> list[Path]:
    targets: list[Path] = []
    for entry in _SCAN_ROOTS:
        path = SRC / entry
        targets.extend([path] if path.is_file() else sorted(path.rglob("*.py")))
    return targets


def test_L3_no_cloud_deployment_identifier_anywhere_in_the_local_product():
    offenders = []
    for path in _scan_targets():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _AWS_IDENTIFIERS.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, ("cloud deployment identifiers in the local product:\n"
                           + "\n".join(offenders))


def test_L3_no_aws_sdk_or_service_vocabulary_in_code():
    offenders = []
    for path in _scan_targets():
        for token in _code_text(path).splitlines():
            if _AWS_VOCABULARY.search(token):
                offenders.append(f"{path.relative_to(SRC)}: {token}")
    assert not offenders, "AWS surface in CODE:\n" + "\n".join(offenders)


@pytest.mark.parametrize("sample", [
    'KEY = "arn:aws:kms:us-east-1:123456789012:key/abc"',
    "url = 'https://x.auth.us-east-1.amazoncognito.com'",
    "URL = 'https://abc.lambda-url.us-east-1.on.aws/'",
    "BUCKET = 's3://nh-brain-prod/m/'",
    "KEY_ID = 'AKIAIOSFODNN7EXAMPLE'",
])
def test_L3_probe_detects_each_class_of_cloud_identifier(sample):
    """The known positives. A grep gate nobody has shown to fire is not a gate."""
    assert _AWS_IDENTIFIERS.search(sample)


@pytest.mark.parametrize("sample", ["boto3", "dynamodb", "assume_role", "sigv4"])
def test_L3_probe_detects_each_class_of_aws_vocabulary(sample):
    assert _AWS_VOCABULARY.search(sample)


def test_L3_the_client_knows_exactly_one_url():
    """Config surface is three keys, and only one of them is an address."""
    from no_human.config import DEFAULT_CONFIG

    assert set(DEFAULT_CONFIG["team_brain"]) == {
        "enabled", "control_plane_url", "max_stale_days"}
    assert DEFAULT_CONFIG["team_brain"]["enabled"] is False
    assert DEFAULT_CONFIG["team_brain"]["control_plane_url"] == ""


def test_L3_the_pinned_key_is_bytes_not_a_key_reference():
    """The corollary: the signing key is PINNED AS BYTES, never fetched.

    A key served by the party that signs is a self-attestation, not a trust
    anchor — and fetching it with an AWS call would be a dependency, while bytes
    are a constant.
    """
    from no_human.brain import keys, verify

    assert keys.PINNED_SIGNING_KEYS, "no key pinned: nothing could ever verify"
    for der in keys.PINNED_SIGNING_KEYS:
        assert isinstance(der, bytes)
        n, e = verify.rsa_public_numbers(der)
        assert n.bit_length() == 2048 and e == 65537
    text = (BRAIN / "keys.py").read_text()
    assert "get_public_key" not in text.replace("get-public-key", "")


def test_L3_no_route_fetches_a_signing_key():
    """There is deliberately no fallback that would let a served key be used."""
    joined = "\n".join(p.read_text() for p in _brain_sources())
    assert "signing-key" not in joined
    assert "PINNED_SIGNING_KEYS" in (BRAIN / "sync.py").read_text()


# --------------------------------------------------------------------------- #
# L4 — inert by default, and removable                                        #
# --------------------------------------------------------------------------- #


def _imported_brain_modules_after(import_line: str) -> list[str]:
    """Import something in a FRESH interpreter and report what it dragged in.

    A subprocess, not ``importlib.reload``: reloading ``no_human.config`` in
    this process mints a new ``AuthError`` class and a new ``DEFAULT_CONFIG``
    object, and reloading ``cli.commands`` re-registers every click group — both
    of which quietly break OTHER tests that are already holding the old objects.
    That is not hypothetical; it is what the first version of these two tests
    did, and it surfaced as three unrelated failures in ``test_config.py`` under
    xdist. A clean interpreter is the only honest way to ask this question.
    """
    import subprocess

    script = (f"import json, sys; {import_line}; "
              "print(json.dumps([m for m in sys.modules "
              "if m.startswith('no_human.brain')]))")
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(SRC.parent)})
    import json
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_L4_importing_the_cli_does_not_import_the_brain():
    """`nh --help` on a machine with the feature off must not load the client."""
    assert _imported_brain_modules_after("import no_human.cli.commands") == []


def test_L4_the_import_probe_can_see_an_import_that_did_happen():
    """Known positive: the probe reports the package when it really is loaded."""
    assert _imported_brain_modules_after("import no_human.brain") != []


#: The verbs ``nh`` exposed before ``brain`` was added. Pinned as a LIST rather
#: than a count because the name of this test promises the verbs survive, and a
#: count survives 43 deletions and one addition without noticing.
_PRE_EXISTING_VERBS = (
    "agents", "approve", "auth", "autonomy", "bench", "blocked", "ci-gate",
    "config", "dashboard", "diff", "docs", "doctor", "eval", "history", "init",
    "investigate", "learnings", "learnings-curate", "logs", "mcp-serve",
    "merge-stack", "onboard", "playbook", "recall", "reject", "reply", "repo",
    "review", "review-comments", "rules", "serve", "shadow", "shell", "skills",
    "start", "status", "stop", "task", "test", "unblock", "wake", "watch",
)


def test_L4_nh_brain_is_registered_and_every_pre_existing_verb_survives():
    """The second half of that name was not tested. The body asserted only
    ``"brain" in cli.commands``, so deleting 42 of the 43 verbs left it green —
    which matters here specifically, because ``brain`` is registered through a
    CUSTOM ``click.Group`` subclass and a mistake in that registration is
    exactly the kind of thing that takes siblings down with it.
    """
    import click

    from no_human.cli.commands import cli

    assert "brain" in cli.commands
    missing = [v for v in _PRE_EXISTING_VERBS if v not in cli.commands]
    assert not missing, f"registering `nh brain` lost pre-existing verbs: {missing}"

    # Present in the mapping is not the same as resolvable. `_LazyBrainGroup`
    # overrides `get_command`, and a group that answered for its siblings too
    # would satisfy the membership check above while breaking every one of them.
    ctx = click.Context(cli)
    unresolvable = [v for v in _PRE_EXISTING_VERBS if cli.get_command(ctx, v) is None]
    assert not unresolvable, f"verbs listed but not resolvable: {unresolvable}"


def _prompt_orchestrator(config_overrides: dict):
    """A real Orchestrator, far enough constructed to build a coder prompt."""
    from no_human.config import DEFAULT_CONFIG, Config
    from no_human.core.orchestrator import Orchestrator

    import copy
    data = copy.deepcopy(DEFAULT_CONFIG)
    data.update(config_overrides)
    config = Config(data=data, path=Path("/nonexistent/config.yaml"))
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = config
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_playbook = None
    orch._repo_hints = None
    orch._active_memories = []
    orch._brain_watermark = None
    return orch


def _coder_prompt(orch):
    from no_human.core.task import Task, TaskStatus
    task = Task(id="brain-test", source="test", title="Add a flag",
                status=TaskStatus.IMPLEMENTING, description="A description",
                acceptance_criteria=["it works"], repo_path="/tmp/repo")
    return orch._build_implement_prompt(task, work_dir="/tmp/repo")


def test_L4_the_feature_being_off_never_imports_the_brain_at_all(monkeypatch):
    """L4's FIRST half: off means the package is never even reached.

    This replaces a test that asserted L4's second half twice over and proved
    neither. It read `with_feature_off == as_if_deleted` under a patched
    ``__import__`` — but with the feature off the early returns fire before any
    import, so the shim never had anything to refuse and the assertion compared
    a prompt with itself. It stayed green with the whole coder-prompt hook
    deleted. The byte-identity property is really tested by
    ``test_L4_the_package_being_deletable_is_exercised_not_assumed``, which
    turns the feature ON so the import actually happens.

    What was left untested is the property this one now asserts, and it is a
    different statement from deletability: with the feature off, ``nh`` must not
    IMPORT the client — not import it and find it inert, not import it and get
    an empty block. That is what keeps ``nh --help`` free of the brain's import
    cost on a machine that never enabled it.

    THE SHIM MATCHES THE REAL IMPORT, which the old one did not.
    ``from ..brain import coder_context`` reaches ``__import__`` as
    ``name="brain"``, ``level=2`` — never as ``"no_human.brain"`` and never with
    ``name == ""``, the only two shapes the old shim tested. It could not have
    fired whatever the code did. The assertion below that it fires on the known
    positive is what stops this one inheriting the same defect.
    """
    asked: list[tuple] = []
    real_import = __import__

    def watch(name, globals=None, locals=None, fromlist=(), level=0):
        package = (globals or {}).get("__package__") or ""
        absolute = name
        if level:
            base = package.rsplit(".", level - 1)[0] if level > 1 else package
            absolute = f"{base}.{name}" if name else base
        if absolute.startswith("no_human.brain") or name.startswith("no_human.brain"):
            asked.append((name, level, absolute))
            raise ImportError("simulated deletion of src/no_human/brain/")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", watch)

    off = _prompt_orchestrator({})
    off.emit = lambda *a, **k: None
    prompt_off = _coder_prompt(off)
    assert off._pin_brain_watermark() is None
    assert asked == [], (
        "the feature is off and the brain package was imported anyway: "
        f"{asked}")

    # KNOWN POSITIVE for the shim itself. Turn the feature on and the very same
    # shim must fire — otherwise the empty list above means nothing at all.
    on = _prompt_orchestrator({"team_brain": {
        "enabled": True, "control_plane_url": "https://x", "max_stale_days": 14}})
    on.emit = lambda *a, **k: None
    prompt_on = _coder_prompt(on)
    assert asked, (
        "the shim never fired even with the feature ON, so it cannot detect an "
        "import and the assertion above is vacuous")

    # And L4's second half still holds through the refused import: a build with
    # the directory deleted produces the same bytes as one with it off.
    assert prompt_on == prompt_off


def test_L4_the_package_being_deletable_is_exercised_not_assumed(tmp_path, monkeypatch):
    """The feature-off path returns before the import, so the test above never
    actually makes the import fail. This one does: feature ON, rules held, and
    ``src/no_human/brain/`` unimportable. The prompt must come back byte-identical
    to the feature-off prompt — which is what "deleting the directory leaves a
    working product" means.
    """
    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    baseline = _coder_prompt(_prompt_orchestrator({"database": {"path": str(db)}}))

    # `sys.modules` is deliberately left alone: the patched ``__import__`` below
    # raises before the cache is ever consulted, so evicting the real modules
    # would only hand other tests a second copy of them.
    real_import = __import__

    def refuse(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("brain") or name.startswith("no_human.brain") or (
                level and "brain" in (fromlist or ())):
            raise ImportError("simulated deletion of src/no_human/brain/")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", refuse)
    orch = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    })
    orch.emit = lambda *a, **k: None
    assert orch._pin_brain_watermark() is None
    assert _coder_prompt(orch) == baseline


def test_L4_the_golden_test_can_detect_a_changed_prompt(tmp_path, monkeypatch):
    """Known positive: with the feature ON and a verified rule held, the prompt
    DOES change. Without this the test above would pass on a client that never
    worked at all."""
    from no_human.brain import store

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)

    off = _coder_prompt(_prompt_orchestrator({}))
    orch = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    })
    orch.emit = lambda *a, **k: None
    orch._brain_watermark = 99
    on = _coder_prompt(orch)
    assert on != off
    assert "TEAM CONVENTIONS" in on
    assert "Prefer explicit imports" in on
    conn = store.connect(db)
    conn.close()


def _seed_injectable_rule(db):
    import time

    from no_human.brain import store

    conn = store.connect(db)
    try:
        store.upsert_rule(conn, {
            "rule_id": "01JZ8QF3M4N5P6R7S8T9V0W1X2", "version": 2,
            "scope": "GLOBAL", "type": "rule",
            "title": "Prefer explicit imports",
            "content": "Import names explicitly rather than with a star import.",
            "visibility": "coder", "status": "active",
        }, injectable=True, refused_reason=None, manifest_version=2,
            approvals=[{"actor": "admin-1"}])
        store.set_state(conn, "last_verified_sync", time.time())
        store.set_state(conn, "watermark", 2)
    finally:
        conn.close()


def test_L4_the_feature_off_injects_nothing_even_with_rules_held(tmp_path):
    """Rules can be synced and stored, and with the flag off none is rendered."""
    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    orch = _prompt_orchestrator({"database": {"path": str(db)}})
    assert "TEAM CONVENTIONS" not in _coder_prompt(orch)


def test_L4_deleting_the_package_leaves_a_working_cli(monkeypatch):
    """`nh brain` degrades to a usage error; nothing else breaks."""
    import click

    from no_human.cli.commands import _LazyBrainGroup

    group = _LazyBrainGroup("brain")
    monkeypatch.setattr(_LazyBrainGroup, "_delegate", lambda self: None)
    assert group.list_commands(None) == []
    with pytest.raises(click.UsageError):
        group.get_command(None, "sync")


# --------------------------------------------------------------------------- #
# L5 — the service is never on a critical path                                #
# --------------------------------------------------------------------------- #


def test_L5_building_a_prompt_opens_no_socket(tmp_path, monkeypatch):
    """Black-hole the network entirely and build a coder prompt with the feature
    ENABLED. Nothing may touch a socket: sync is explicit, and a running task
    reads only local SQLite."""
    import socket

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)

    def explode(*_a, **_k):
        raise AssertionError("the brain opened a socket on a task's critical path")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    orch = _prompt_orchestrator({
        "team_brain": {"enabled": True,
                       "control_plane_url": "https://192.0.2.1",  # black hole
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    })
    orch.emit = lambda *a, **k: None
    orch._brain_watermark = 99
    prompt = _coder_prompt(orch)
    assert "TEAM CONVENTIONS" in prompt


def test_L5_a_broken_brain_store_still_produces_a_prompt(monkeypatch):
    """Every failure degrades to today's behaviour — zero remote rules."""
    import no_human.brain as brain

    monkeypatch.setattr(brain, "coder_context",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    orch = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": "/nonexistent/nowhere/no_human.db"},
    })
    prompt = _coder_prompt(orch)
    assert "Acceptance criteria" in prompt
    assert "TEAM CONVENTIONS" not in prompt


def test_L5_an_absent_database_is_not_an_error():
    """A machine that has never synced. This is the ABSENT case, and it returns
    at the `_readable` check without ever opening anything — which is why it
    cannot stand in for the unreadable one below."""
    from no_human.brain import coder_context, pin_watermark
    from no_human.config import Config

    config = Config(data={"team_brain": {"enabled": True,
                                         "control_plane_url": "https://x",
                                         "max_stale_days": 14},
                          "database": {"path": "/proc/nonexistent/x.db"}},
                    path=Path("/nonexistent"))
    assert pin_watermark(config) is None
    assert coder_context(config, 5).block == ""


def test_L5_an_unreadable_database_is_not_an_error(tmp_path):
    """The case the test above was NAMED for and did not reach.

    ``/proc/nonexistent/x.db`` does not exist, so ``_readable`` returned False
    and both entry points returned before opening anything — the total
    ``except Exception`` that L5 actually rests on was never executed. Here the
    file EXISTS and is not a database, so ``connect_readonly`` gets as far as a
    query and raises, which is the path a corrupt store on a real machine takes.
    """
    from no_human.brain import coder_context, pin_watermark
    from no_human.config import Config

    db = tmp_path / "no_human.db"
    db.write_bytes(b"this is not a sqlite database, not even close\n" * 64)

    config = Config(data={"team_brain": {"enabled": True,
                                         "control_plane_url": "https://x",
                                         "max_stale_days": 14},
                          "database": {"path": str(db)}},
                    path=Path("/nonexistent"))
    assert pin_watermark(config) is None
    assert coder_context(config, 5).block == ""


# --------------------------------------------------------------------------- #
# A1-A5 — the auth separation                                                 #
# --------------------------------------------------------------------------- #


def test_A1_the_brain_credential_is_never_placed_in_the_environment(tmp_path, monkeypatch):
    """The sharp line. The Claude token IS exported into os.environ on purpose
    (the Agent SDK subprocess must inherit it); a brain token must never be,
    because generated code inside the coder's sandbox can read that environment.
    """
    from no_human.brain import credentials

    monkeypatch.setenv("NO_HUMAN_HOME_OVERRIDE", str(tmp_path / "home"))
    secret = "REFRESH-TOKEN-SENTINEL-9f2a"
    id_secret = "ID-TOKEN-SENTINEL-4b7c"
    before = dict(os.environ)
    credentials.save(credentials.Credential(secret, id_secret, 0.0, "t", "s"))
    loaded = credentials.load()
    assert loaded is not None and loaded.refresh_token == secret

    for key, value in os.environ.items():
        assert secret not in str(value), f"{key} carries the refresh token"
        assert id_secret not in str(value), f"{key} carries the id token"
    assert set(os.environ) == set(before)


def test_A1_no_module_in_the_package_writes_to_the_environment():
    """Structural, not just observational: nothing here assigns os.environ."""
    for path in _brain_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                source = ast.unparse(node.value)
                assert "environ" not in source, f"{path.name} writes os.environ"
            if isinstance(node, ast.Call):
                target = ast.unparse(node.func)
                assert target not in ("os.environ.setdefault",
                                      "os.environ.update",
                                      "os.putenv"), f"{path.name}: {target}"


def test_A2_the_claude_auth_path_is_untouched():
    """`config.py`'s auth handling is not modified and knows nothing of the brain.

    METERED_AUTH_VARS is frozen here deliberately: that list exists so a run
    bills exactly ONE inference path, and a Cognito token is not a billing path.
    Extending it would teach the next reader that it is one.
    """
    import inspect

    from no_human import config

    assert config.METERED_AUTH_VARS == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_BEARER_TOKEN_BEDROCK",
    )
    for fn in (config.scrub_metered_auth, config.assert_subscription_mode,
               config.load_env_token, config._read_env_file,
               config._reject_api_key_in_config):
        assert "brain" not in inspect.getsource(fn).lower()
    assert "no_human.brain" not in Path(config.__file__).read_text()


def test_A2_config_module_does_not_import_the_brain():
    assert _imported_brain_modules_after("import no_human.config") == []


def test_A3_a_request_carrying_claude_credential_material_is_refused():
    """The outbound guard, with its known positives. Nothing in this package can
    reach a Claude credential; this exists so that if something ever does, the
    request fails here instead of succeeding at the service."""
    from no_human.brain import client

    client._guard_outbound({"authorization": "Bearer eyJhbGciOi"}, None)  # fine
    for leak in ("sk-ant-oat01-abc", "sk-ant-api03-abc"):
        with pytest.raises(client.BrainCredentialLeak):
            client._guard_outbound({"authorization": f"Bearer {leak}"}, None)
    with pytest.raises(client.BrainCredentialLeak):
        client._guard_outbound({}, "CLAUDE_CODE_OAUTH_TOKEN=x")
    with pytest.raises(client.BrainCredentialLeak):
        client._guard_outbound({}, b"ANTHROPIC_API_KEY=x")


def test_A3_the_first_increment_sends_no_bodies_to_the_control_plane():
    """Read-only is the increment where a bug CANNOT leak the customer's IP:
    nothing but a sign-in token leaves the machine. The only POSTs in the client
    are the OAuth token exchange and refresh, and both go to the identity
    provider with a fixed field set."""
    text = (BRAIN / "client.py").read_text()
    posts = re.findall(r'_request\(\s*"POST"', text)
    assert len(posts) == 2, "a new write path appeared; it is out of scope"
    code = _code_text(BRAIN / "client.py")
    assert "proposals" not in code and "vote" not in code


def test_A4_every_brain_command_bootstraps_without_the_claude_auth_assertion():
    """A broken Claude credential must not block brain administration, and
    `assert_subscription_mode` must never be invoked by a brain command."""
    code = _code_text(BRAIN / "cli.py")
    assert "require_auth" in code and "False" in code
    assert "assert_subscription_mode" not in code
    assert "require_auth=True" not in (BRAIN / "cli.py").read_text()
    assert code.count("_bootstrap") == 2, (  # the import and the one call
        "every subcommand must go through the single _cfg() helper")


def _tls_verify_offenders(tree: ast.AST) -> list[str]:
    """Every idiom by which ``verify=`` could reach an httpx constructor.

    THE PROBE MUST MATCH THE UNIVERSE OF THE CODE IT GUARDS. What this replaces
    walked calls and asserted ``kw.arg != "verify"``, which sees exactly one
    idiom — the literal ``httpx.Client(verify=False)``. ``client.py`` is not
    written that way. It builds a ``kwargs`` dict and unpacks it, and in a
    ``**kwargs`` node ``kw.arg is None``, so the guard was blind to the only
    shape the one file it existed for actually uses. Adding ``"verify": False``
    to that dict turned TLS verification off with all 176 tests green, while the
    literal form still fired — which is what made the guard look alive.

    So the net is cast on where the value is WRITTEN, not only on where it is
    passed: a ``verify`` key in any dict literal, any ``d["verify"] = ...``, any
    ``setdefault``/``update`` that mentions it, as well as the literal keyword.
    This is a tripwire and it is not the gate — a computed key
    (``d["ver"+"ify"]``) defeats any syntactic check. The gate is
    ``test_A5_no_brain_request_ever_builds_a_client_with_verify``, which watches
    the constructor itself and cannot be defeated by an idiom at all.
    """
    offences: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "verify":
                    offences.append(f"verify= passed to {ast.unparse(node.func)}")
            if isinstance(node.func, ast.Attribute) and \
                    node.func.attr in ("setdefault", "update") and \
                    "verify" in ast.unparse(node):
                offences.append(f"{ast.unparse(node.func)}(...) may set verify")
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "verify":
                    offences.append("a dict literal carries a 'verify' key")
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) \
                and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "verify":
            offences.append(f"{ast.unparse(node.value)}['verify'] is assigned")
    return offences


def test_A5_tls_verification_is_never_disabled():
    """Recorded because the tree contains a counter-example: vcs/pr_watcher.py
    constructs an httpx client with verify=False. That must not be copied."""
    for path in _brain_sources():
        offences = _tls_verify_offenders(ast.parse(path.read_text()))
        assert offences == [], f"{path.name}: {offences}"


@pytest.mark.parametrize("source", [
    # The literal form — all the old probe could see.
    'import httpx\nhttpx.Client(verify=False)\n',
    # THE IDIOM client.py IS ACTUALLY WRITTEN IN, and the one that shipped past
    # the old probe. `kw.arg is None` for `**kwargs`, so the keyword walk saw
    # nothing at all.
    'import httpx\n'
    'kwargs = {"timeout": 15.0, "verify": False}\n'
    'with httpx.Client(**kwargs) as c:\n    pass\n',
    # Built up a statement at a time.
    'kwargs = {}\nkwargs["verify"] = False\n',
    'kwargs = {}\nkwargs.setdefault("verify", False)\n',
    'kwargs = {}\nkwargs.update({"verify": False})\n',
])
def test_A5_the_structural_probe_fires_on_every_idiom(source):
    """KNOWN POSITIVES for the probe above. A structural claim needs a
    structural instrument, and an instrument that has never been shown to fire
    is indistinguishable from no instrument: the previous A5 test passed on a
    tree where TLS was off. Each source here is a real way to disable
    verification and each must be detected."""
    assert _tls_verify_offenders(ast.parse(source)), source


def test_A5_the_structural_probe_does_not_fire_on_the_verifier_module():
    """The negative half. `brain/verify.py` is full of the word `verify` in
    function and module names, and a probe that matched those would be deleted
    within a week for crying wolf."""
    assert _tls_verify_offenders(ast.parse((BRAIN / "verify.py").read_text())) == []


# --------------------------------------------------------------------------- #
# The gates, driven through the REAL request path                             #
#                                                                             #
# Every test above this line either reads source text or monkeypatches         #
# `client.sync_delta` / `client.manifest` — which are the two functions that   #
# CALL `_request`, so nothing in the suite ever executed `_request` itself.    #
# That is why disabling TLS and deleting the outbound-credential chokepoint,   #
# together, left all 176 tests passing: neither gate was on any tested path.   #
#                                                                             #
# `_request` already threads a `transport=` argument through every function in #
# the module, for exactly this. `httpx.MockTransport` plugs into it and the    #
# real client code runs end to end — real header assembly, real guard call,    #
# real client construction, real status handling. Every gate below is proven   #
# red under mutation in the commit message.                                    #
# --------------------------------------------------------------------------- #


def _brain_cfg(url: str = "https://control.example"):
    from no_human.brain.settings import BrainConfig
    return BrainConfig(True, url, 14)


def _recording_transport(status: int = 200, payload=None):
    """A real httpx transport that records what actually left the client."""
    import httpx
    seen: list = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={} if payload is None else payload)

    return httpx.MockTransport(handler), seen


def test_A5_no_brain_request_ever_builds_a_client_with_verify(monkeypatch):
    """THE GATE the structural probe is only a tripwire for.

    Watches `httpx.Client` itself, so it is blind to no idiom: literal keyword,
    `**kwargs`, or a key computed at runtime all arrive here as a constructor
    kwarg. Red under the mutation that shipped past the AST walk.
    """
    import httpx

    from no_human.brain import client

    seen_kwargs: list[dict] = []
    real_client = httpx.Client

    class Recording(real_client):
        def __init__(self, *args, **kwargs):
            seen_kwargs.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", Recording)
    transport, _seen = _recording_transport(200, {"ok": True})
    assert client.health(_brain_cfg(), transport=transport) is True

    # KNOWN POSITIVE: the probe really did observe a client being constructed.
    # Without this the assertion below passes on an empty list, which is the
    # same nothing the old test was measuring.
    assert seen_kwargs, "no httpx.Client was constructed; the probe saw nothing"
    for kwargs in seen_kwargs:
        assert "verify" not in kwargs, (
            f"a brain request built an httpx client with verify={kwargs['verify']!r}")


def test_A3_the_outbound_chokepoint_is_on_the_real_request_path():
    """`_guard_outbound` has unit tests; NONE of them ran through `_request`.

    Commenting out the single call site left the suite green, so the guard's
    coverage proved that the matcher works and nothing at all about whether it
    is invoked. This drives a credential-shaped bearer through the real
    `sync_delta` and asserts both halves: the call raises, AND nothing reached
    the transport.
    """
    from no_human.brain import client

    transport, seen = _recording_transport(200, {"items": []})
    with pytest.raises(client.BrainCredentialLeak):
        client.sync_delta(_brain_cfg(), "sk-ant-oat01-not-a-real-token", 0,
                          transport=transport)
    assert seen == [], "the request carrying credential material was still sent"

    # KNOWN POSITIVE: an ordinary bearer is not blocked, so the test above is
    # measuring the guard and not a client that refuses everything.
    client.sync_delta(_brain_cfg(), "eyJhbGciOiJSUzI1NiJ9.e30.x", 0,
                      transport=transport)
    assert len(seen) == 1


def test_the_409_resync_trigger_is_reachable_through_the_real_client():
    """`if status == 409:` → `if False:` broke nothing. The 409 is the TRIGGER
    for the entire replay-defence design — the high-water mark, `chain_top`,
    `keep_high_water` — and no test drove it through the client that raises it.

    STATED HONESTLY, because it changes what this test is worth: the 409 branch
    is very nearly an EQUIVALENT MUTANT of the line after it. Replace it with
    `if False:` and the generic `status != 200` check below still raises
    `BrainHTTPError` with `status=409`, so `sync.run`'s `exc.status == 409` test
    still fires and the resync still happens. The only thing the branch adds is
    the `resync_required` message. So this test asserts the message — the one
    observable the branch owns — and the real gate for the trigger is
    `test_a_wire_409_drives_the_real_client_into_a_full_resync`, which runs the
    whole path end to end.
    """
    from no_human.brain import client

    transport, _seen = _recording_transport(409, {"error": "resync_required"})
    with pytest.raises(client.BrainHTTPError) as caught:
        client.sync_delta(_brain_cfg(), "token", 0, transport=transport)
    assert caught.value.status == 409
    assert "resync_required" in str(caught.value)

    # KNOWN POSITIVE: a 200 on the same path does not raise.
    ok_transport, _ = _recording_transport(200, {"items": [], "watermark": 0})
    assert client.sync_delta(_brain_cfg(), "token", 0,
                             transport=ok_transport) == {"items": [],
                                                         "watermark": 0}


def test_a_404_manifest_is_absent_and_a_500_manifest_is_an_error():
    """The two branches the whole availability-vs-trust split rests on, through
    the real client: 404 means "no admission at this version" and must return
    None, 5xx means "not verifiable yet" and must raise."""
    from no_human.brain import client

    none_transport, _ = _recording_transport(404, {})
    assert client.manifest(_brain_cfg(), "token", 3,
                           transport=none_transport) is None

    err_transport, _ = _recording_transport(503, {})
    with pytest.raises(client.BrainHTTPError):
        client.manifest(_brain_cfg(), "token", 3, transport=err_transport)


@pytest.mark.parametrize("url", [
    "http://control.example",           # plaintext to a remote host
    "http://localhost.evil.com",        # the prefix trap named in _base's comment
    "http://127.0.0.1@evil.com",        # userinfo, so the real host is evil.com
    "ftp://control.example",
])
def test_a_non_https_control_plane_is_refused_before_any_request(url):
    """`_base` is a gate and it is on the real path — nothing may be sent to a
    control plane reached over plaintext. Paired with its known positives."""
    from no_human.brain import client

    transport, seen = _recording_transport(200, {})
    with pytest.raises(client.BrainHTTPError):
        client.health(_brain_cfg(url), transport=transport)
    assert seen == [], f"{url} was actually contacted"


@pytest.mark.parametrize("url", ["https://control.example",
                                 "http://localhost:8000", "http://127.0.0.1:9"])
def test_https_and_real_loopback_are_accepted(url):
    """KNOWN POSITIVE for the test above: a verifier that refused every URL
    would pass it."""
    from no_human.brain import client

    transport, seen = _recording_transport(200, {})
    assert client.health(_brain_cfg(url), transport=transport) is True
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# The sign-in gates                                                            #
# --------------------------------------------------------------------------- #


def test_the_oauth_state_check_rejects_a_callback_it_did_not_originate():
    """CSRF. Replacing the whole condition with `if False:` left the suite green
    because the check lived inside a handler class nested inside
    `wait_for_code`, reachable only by binding port 8422. It is now a pure
    module-level function and this is its gate: a callback carrying somebody
    else's `state` is somebody else's authorization code."""
    from no_human.brain import credentials

    state = "the-state-this-process-generated"
    code, error = credentials.validate_callback(
        {"state": [state], "code": ["auth-code-1"]}, state)
    assert (code, error) == ("auth-code-1", "")          # KNOWN POSITIVE

    for hostile in ({"state": ["forged"], "code": ["auth-code-1"]},
                    {"code": ["auth-code-1"]},
                    {"state": [""], "code": ["auth-code-1"]},
                    {"state": [state + "x"], "code": ["auth-code-1"]}):
        got, err = credentials.validate_callback(hostile, state)
        assert got == "" and err, hostile

    # An empty EXPECTED state must not become a wildcard that matches a
    # callback with no state at all.
    assert credentials.validate_callback({"code": ["c"]}, "") == ("", "state mismatch")
    # Right state, no code, is still a failure.
    assert credentials.validate_callback({"state": [state]}, state)[0] == ""


def test_the_loopback_listener_really_applies_the_state_check(monkeypatch):
    """End to end over a real socket, because a pure function has the same
    defect one level up: delete the CALL and the unit tests above still pass.
    Binds an ephemeral port rather than 8422 so it cannot collide under -n."""
    import socket
    import threading
    import urllib.request

    from no_human.brain import credentials

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(credentials, "LOOPBACK_PORT", port)

    def drive(query):
        result = {}

        def serve():
            try:
                result["code"] = credentials.wait_for_code("REAL-STATE", timeout=10)
            except credentials.BrainAuthError as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        for _ in range(200):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/callback?{query}", timeout=5).read()
                break
            except OSError:
                time.sleep(0.02)
        thread.join(timeout=15)
        return result

    assert drive("state=REAL-STATE&code=good-code").get("code") == "good-code"
    assert "code" not in drive("state=FORGED&code=attacker-code")


@pytest.mark.parametrize("endpoint", ["http://idp.example/authorize", "",
                                      "ftp://idp.example", "//idp.example"])
def test_a_non_https_sign_in_endpoint_is_refused(endpoint):
    """The control plane supplies these endpoints by discovery, so they are
    server-authored: a compromised control plane must not be able to send the
    browser to a plaintext endpoint."""
    from no_human.brain import credentials

    with pytest.raises(credentials.BrainAuthError):
        credentials.authorize_url({"authorization_endpoint": endpoint,
                                   "client_id": "abc"}, "state", "challenge")


def test_a_missing_client_id_is_refused_and_a_good_config_builds_a_url():
    """KNOWN POSITIVE for the test above."""
    from no_human.brain import credentials

    with pytest.raises(credentials.BrainAuthError):
        credentials.authorize_url(
            {"authorization_endpoint": "https://idp.example/authorize"},
            "state", "challenge")
    url = credentials.authorize_url(
        {"authorization_endpoint": "https://idp.example/authorize",
         "client_id": "abc"}, "the-state", "the-challenge")
    assert url.startswith("https://idp.example/authorize?")
    assert "state=the-state" in url and "code_challenge=the-challenge" in url
    assert "code_challenge_method=S256" in url
    assert f"redirect_uri=http%3A%2F%2Flocalhost%3A{credentials.LOOPBACK_PORT}" in url


def test_a_non_https_token_endpoint_is_refused_before_the_code_is_sent():
    """The authorization code is a credential. It must not be POSTed in the
    clear to an endpoint a compromised control plane chose."""
    from no_human.brain import client

    transport, seen = _recording_transport(200, {"id_token": "x"})
    with pytest.raises(client.BrainHTTPError):
        client.exchange_code({"token_endpoint": "http://idp.example/token",
                              "client_id": "abc"},
                             "the-code", "verifier", "redirect",
                             transport=transport)
    assert seen == [], "the authorization code was sent over plaintext"


@pytest.mark.parametrize("expires_in, valid", [
    (10_000, True),      # comfortably live
    (0, False),          # expired
    (-10_000, False),    # long expired
    (60, False),         # inside the 120s refresh skew
])
def test_an_id_token_is_only_valid_before_its_expiry_minus_the_skew(expires_in, valid):
    """`id_token_valid` gates whether a cached token is reused or refreshed.
    Dropping the expiry comparison entirely left the suite green, so a token
    that expired months ago would have been sent as a bearer forever."""
    from no_human.brain import credentials

    cred = credentials.Credential("refresh", "id-token",
                                  time.time() + expires_in, "team", "subject")
    assert cred.id_token_valid is valid


def test_an_absent_id_token_is_never_valid():
    """The other half: no token is not a fresh token."""
    from no_human.brain import credentials

    assert not credentials.Credential("refresh", "", time.time() + 10_000,
                                      "t", "s").id_token_valid


def test_A6_the_attempt_records_both_who_paid_and_what_it_knew():
    """Two columns, because they answer different questions."""
    text = (SRC / "core" / "db.py").read_text()
    assert '"auth_profile": "TEXT"' in text
    assert '"brain_watermark": "INTEGER"' in text


# --------------------------------------------------------------------------- #
# L5, on the DATABASE — the half the network black-hole above cannot see      #
# --------------------------------------------------------------------------- #
#
# `test_L5_building_a_prompt_opens_no_socket` black-holes the NETWORK, and the
# brain never touches the network on a task's path anyway. What it does touch,
# on every attempt and on every implement-prompt build, is the product's LIVE
# SQLite file — and it opened that with `timeout=10.0` and ran four
# `CREATE TABLE IF NOT EXISTS` statements, which need a WRITE lock. With another
# writer holding one, the coder's critical path stalled for ten seconds and then
# produced an empty block anyway. That is exactly what L5 forbids, and the
# reason it shipped is that no test here ever locked the database.

_LOCK_BUDGET_SECONDS = 3.0


def _hold_exclusive_lock(db):
    """A second connection holding a real write lock on the same file."""
    import sqlite3

    holder = sqlite3.connect(str(db), timeout=30.0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_L5_a_locked_database_does_not_block_the_coder_path(tmp_path):
    """The read path must give up in well under a second, not in ten."""
    import time as _time

    from no_human.brain import coder_context, pin_watermark

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    config = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    }).config

    holder = _hold_exclusive_lock(db)
    try:
        start = _time.monotonic()
        watermark = pin_watermark(config)
        pinned = _time.monotonic() - start
        start = _time.monotonic()
        ctx = coder_context(config, watermark if watermark is not None else 99)
        rendered = _time.monotonic() - start
    finally:
        holder.rollback()
        holder.close()

    assert pinned < _LOCK_BUDGET_SECONDS, (
        f"pin_watermark blocked the attempt for {pinned:.2f}s on a locked "
        "database")
    assert rendered < _LOCK_BUDGET_SECONDS, (
        f"coder_context blocked the coder prompt for {rendered:.2f}s on a "
        "locked database")
    assert ctx.block == ""  # degrades to today's behaviour: zero remote rules


def test_L5_a_locked_database_still_produces_a_coder_prompt(tmp_path):
    """End to end through the orchestrator, which is where the ten seconds were
    actually being spent."""
    import time as _time

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    orch = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    })
    orch.emit = lambda *a, **k: None
    orch._brain_watermark = 99

    holder = _hold_exclusive_lock(db)
    try:
        start = _time.monotonic()
        prompt = _coder_prompt(orch)
        elapsed = _time.monotonic() - start
    finally:
        holder.rollback()
        holder.close()

    assert elapsed < _LOCK_BUDGET_SECONDS, (
        f"the implement prompt took {elapsed:.2f}s to build because the brain "
        "was waiting on a database lock")
    assert "Acceptance criteria" in prompt
    assert "TEAM CONVENTIONS" not in prompt


def test_L5_the_lock_probe_really_blocks_a_writer(tmp_path):
    """Known positive. Without this the two tests above would pass on a lock
    that was never actually held, which is the same shape of mistake as testing
    a tracked-file verifier with an untracked file."""
    import sqlite3

    from no_human.brain import store

    import time as _time

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    holder = _hold_exclusive_lock(db)
    try:
        writer = sqlite3.connect(str(db), timeout=0.2)
        with pytest.raises(sqlite3.OperationalError):
            writer.execute("CREATE TABLE IF NOT EXISTS probe_table (x INTEGER)")
            writer.commit()
        writer.close()

        # The lock reaches the READ path too — a reader that could sail past it
        # would make the timing tests above prove nothing — and the quarter
        # second is what bounds the wait. Behavioural, not a comparison of two
        # function objects.
        conn = store.connect_readonly(db)
        start = _time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.watermark(conn)
        elapsed = _time.monotonic() - start
        conn.close()
        assert elapsed < 1.0, f"the read path waited {elapsed:.2f}s"
    finally:
        holder.rollback()
        holder.close()


def test_L5_the_task_read_path_runs_no_ddl_and_creates_nothing(tmp_path):
    """A task's path may not issue DDL against the live database at all. Point
    it at a database with no brain tables and none may appear."""
    import sqlite3

    from no_human.brain import coder_context, pin_watermark

    db = tmp_path / "no_human.db"
    plain = sqlite3.connect(str(db))
    plain.execute("CREATE TABLE unrelated (x INTEGER)")
    plain.commit()
    plain.close()

    config = _prompt_orchestrator({
        "team_brain": {"enabled": True, "control_plane_url": "https://x",
                       "max_stale_days": 14},
        "database": {"path": str(db)},
    }).config
    assert pin_watermark(config) is None
    assert coder_context(config, 5).block == ""

    check = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        check.close()
    assert names == {"unrelated"}, f"the read path created tables: {names}"


def test_L5_the_read_path_cannot_write_even_if_it_tried(tmp_path):
    """Structural, not observational: the connection a task gets is query-only,
    so a future edit that adds a write there fails loudly rather than taking a
    write lock on the coder's critical path."""
    import sqlite3

    from no_human.brain import store

    db = tmp_path / "no_human.db"
    _seed_injectable_rule(db)
    conn = store.connect_readonly(db)
    try:
        assert store.watermark(conn) == 2  # reading works
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO brain_state VALUES ('k', 'v')")
    finally:
        conn.close()


def test_L5_the_read_path_never_waits_the_write_paths_timeout():
    """The two timeouts are different numbers on purpose, and the read one is
    the one on a task's path."""
    from no_human.brain import store

    assert store.READ_TIMEOUT_SECONDS <= 0.5
    assert store.READ_TIMEOUT_SECONDS < store.WRITE_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# The source scan itself — every structural gate above is only as wide as this #
# --------------------------------------------------------------------------- #


def test_the_source_scan_reaches_a_subpackage(tmp_path):
    """`glob("*.py")` made L1, L2, A1 and A5 all blind to any subpackage: a file
    in `brain/bg/` evaded four invariants at once."""
    (tmp_path / "bg").mkdir()
    (tmp_path / "bg" / "worker.py").write_text("import threading\n")
    (tmp_path / "top.py").write_text("x = 1\n")
    found = {p.name for p in _brain_sources(tmp_path)}
    assert found == {"worker.py", "top.py"}, found


def test_the_structural_gates_see_a_violation_in_a_real_subpackage(tmp_path):
    """The known positive: the REAL gate, over a REAL copy of the REAL package,
    with the evasion the reviewer used planted in a subdirectory of it.

    A COPY, and this matters. The first version of this test planted the file in
    ``src/no_human/brain/`` itself. The suite runs under xdist with ``--dist
    load``, so a sibling worker executing the other invariant tests would read
    the planted file and go red for no reason of its own — and a hard kill
    mid-test leaves the violation sitting in the source tree. A test that
    mutates shared state to prove a point about shared state is the wrong
    trade; copying costs a few milliseconds and removes the whole class.
    """
    import shutil

    mirror = tmp_path / "brain"
    shutil.copytree(BRAIN, mirror,
                    ignore=shutil.ignore_patterns("__pycache__"))

    # Clean to start with — otherwise "it fails after the plant" says nothing.
    _assert_starts_nothing(_brain_sources(mirror))

    (mirror / "bg").mkdir()
    (mirror / "bg" / "__init__.py").write_text("")
    (mirror / "bg" / "worker.py").write_text(
        "import threading\n"
        "def go():\n"
        "    threading.Thread(target=print).start()\n")

    assert mirror / "bg" / "worker.py" in set(_brain_sources(mirror)), (
        "the scan did not reach the subpackage at all")
    with pytest.raises(AssertionError, match="worker.py"):
        _assert_starts_nothing(_brain_sources(mirror))

    # And the real tree was never touched.
    assert not (BRAIN / "bg").exists()
    _assert_starts_nothing(_brain_sources())


def test_L2_probe_detects_a_dynamically_imported_module():
    """`import threading` was caught; `__import__("threading")` and
    `importlib.import_module("subprocess")` were not, and both start exactly the
    thing L2 forbids."""
    for source in ('__import__("threading").Thread(target=f).start()\n',
                   'import importlib\n'
                   'importlib.import_module("subprocess").Popen(["x"])\n',
                   'from importlib import import_module\n'
                   'import_module("multiprocessing")\n'):
        tree = ast.parse(source)
        assert _module_names(tree) & _FORBIDDEN_MODULES, source


def test_L2_an_unanalysable_dynamic_import_is_refused():
    """A dynamic import whose name is not a literal cannot be screened, so it is
    treated as the violation it might be rather than as the absence of one."""
    tree = ast.parse('name = "threa" + "ding"\n__import__(name)\n')
    assert _module_names(tree) & _FORBIDDEN_MODULES


def test_L2_the_dynamic_probe_does_not_fire_on_an_allowed_module():
    """Known negative: the widened scan is not simply flagging every call."""
    tree = ast.parse('import importlib\n'
                     'importlib.import_module("json")\n'
                     '__import__("hashlib")\n')
    assert not (_module_names(tree) & _FORBIDDEN_MODULES)


@pytest.mark.parametrize("sender", ["exchange_code", "refresh_tokens"])
@pytest.mark.parametrize("endpoint", [
    "http://attacker.example/token",     # plaintext
    "http://localhost.evil.com/token",   # the loopback-prefix trap
    "https://",                          # scheme only, no host
    "",
    "ftp://idp.example/token",
])
def test_no_oauth_secret_is_sent_to_a_non_https_token_endpoint(sender, endpoint):
    """BOTH senders, because only one of them used to check.

    `token_endpoint` comes from the control plane's own discovery document, so
    it is server-authored. `exchange_code` validated the scheme; `refresh_tokens`
    did not — and it is the worse one to leave open, because the code exchange
    sends a one-time authorization code while the refresh sends the LONG-LIVED
    refresh token, on every sync. Reproduced before the fix:

        SENT IN CLEARTEXT -> http://attacker.example/token
           body: grant_type=refresh_token&client_id=abc&refresh_token=...
    """
    from no_human.brain import client

    transport, seen = _recording_transport(200, {"id_token": "x"})
    auth = {"token_endpoint": endpoint, "client_id": "abc"}
    args = (("the-code", "verifier", "redirect")
            if sender == "exchange_code" else ("THE-REFRESH-TOKEN",))
    with pytest.raises(client.BrainHTTPError):
        getattr(client, sender)(auth, *args, transport=transport)
    assert seen == [], f"{sender} sent a secret to {endpoint!r}"


@pytest.mark.parametrize("sender", ["exchange_code", "refresh_tokens"])
def test_both_token_senders_work_against_a_real_https_endpoint(sender):
    """KNOWN POSITIVE. A pair of functions that refused every endpoint would
    satisfy the ten cases above."""
    from no_human.brain import client

    transport, seen = _recording_transport(200, {"id_token": "an-id-token"})
    auth = {"token_endpoint": "https://idp.example/token", "client_id": "abc"}
    args = (("the-code", "verifier", "redirect")
            if sender == "exchange_code" else ("THE-REFRESH-TOKEN",))
    assert getattr(client, sender)(auth, *args,
                                   transport=transport)["id_token"] == "an-id-token"
    assert len(seen) == 1


@pytest.mark.parametrize("sender", ["exchange_code", "refresh_tokens"])
def test_a_missing_client_id_stops_both_token_senders(sender):
    """The other half of the shared check, on both callers."""
    from no_human.brain import client

    transport, seen = _recording_transport(200, {"id_token": "x"})
    auth = {"token_endpoint": "https://idp.example/token"}
    args = (("the-code", "verifier", "redirect")
            if sender == "exchange_code" else ("THE-REFRESH-TOKEN",))
    with pytest.raises(client.BrainHTTPError):
        getattr(client, sender)(auth, *args, transport=transport)
    assert seen == []
