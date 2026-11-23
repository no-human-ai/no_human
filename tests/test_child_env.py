"""`agent/child_env.py`: the launcher's ambient secrets never reach a coder
subprocess, by env-var NAME shape, in both the additive (Claude/SDK) and the
full-copy (Codex/subprocess) shapes of a child environment."""
from no_human.agent.child_env import (
    CLAUDE_CHILD_KEEP,
    CODEX_CHILD_KEEP,
    drop_foreign_secrets,
    is_foreign_secret,
    is_secret_env_name,
    scrub_foreign_secrets_into,
)

_AMBIENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/op",
    "GIT_ASKPASS": "/usr/bin/true",            # operational, not a credential
    "GITHUB_TOKEN": "ghp_x",
    "gitlab_token": "glpat_x",                  # lower-case is still a token
    "AWS_SECRET_ACCESS_KEY": "aws_x",
    "AWS_PROFILE": "work",                      # the pointer to a credential file
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
    "JIRA_API_TOKEN": "jira_x",                 # what load_env_var exported
    "MY_VENDOR_APIKEY": "v_x",                  # unknown vendor, caught by shape
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth_x",
    "anthropic_api_key": "sk_x",                # lower-case Anthropic name is still Anthropic
    "NO_HUMAN_AGENT_SESSION_KEY": "1",          # hypothetical mark-family name
    "OPENAI_API_KEY": "sk_openai",
}


def test_secret_shape_is_name_based_and_case_insensitive():
    assert is_secret_env_name("GITHUB_TOKEN")
    assert is_secret_env_name("gitlab_token")
    assert is_secret_env_name("AWS_PROFILE")
    assert is_secret_env_name("SSH_AUTH_SOCK")
    assert not is_secret_env_name("PATH")
    assert not is_secret_env_name("GIT_ASKPASS")  # "ASKPASS" is not PASSWORD/PASSWD
    # A passphrase is a credential too (GPG/SSH-key/backup borg/restic/age) and
    # none of the other markers catch `*_PASSPHRASE`.
    assert is_secret_env_name("GPG_PASSPHRASE")
    assert is_secret_env_name("ssh_key_passphrase")
    assert is_foreign_secret("BORG_PASSPHRASE", CLAUDE_CHILD_KEEP, "x")


def test_keep_list_is_case_insensitive_like_the_shape_test():
    assert not is_foreign_secret("ANTHROPIC_API_KEY", CLAUDE_CHILD_KEEP)
    assert not is_foreign_secret("anthropic_api_key", CLAUDE_CHILD_KEEP)
    assert is_foreign_secret("OPENAI_API_KEY", CLAUDE_CHILD_KEEP)
    assert not is_foreign_secret("OPENAI_API_KEY", CODEX_CHILD_KEEP)
    assert is_foreign_secret("ANTHROPIC_API_KEY", CODEX_CHILD_KEEP)


def test_additive_scrub_overrides_foreign_secrets_to_empty_and_nothing_else():
    env = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1", "GITHUB_TOKEN": "deliberate"}
    blanked = scrub_foreign_secrets_into(env, _AMBIENT)
    # Deliberate entries already in `env` win, even when secret-shaped.
    assert env["GITHUB_TOKEN"] == "deliberate"
    assert "GITHUB_TOKEN" not in blanked
    assert sorted(blanked) == sorted([
        "gitlab_token", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "SSH_AUTH_SOCK",
        "GOOGLE_APPLICATION_CREDENTIALS", "JIRA_API_TOKEN", "MY_VENDOR_APIKEY",
        "OPENAI_API_KEY",
    ])
    assert all(env[name] == "" for name in blanked)
    # Kept names and non-secrets are NOT written into the additive mapping:
    # the child inherits their real value from the launcher.
    for untouched in ("PATH", "HOME", "GIT_ASKPASS", "CLAUDE_CODE_OAUTH_TOKEN",
                      "anthropic_api_key", "NO_HUMAN_AGENT_SESSION_KEY"):
        assert untouched not in env, untouched
    # The effective child environment, as the SDK builds it.
    child = {**_AMBIENT, **env}
    assert child["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth_x"
    assert child["AWS_SECRET_ACCESS_KEY"] == ""
    assert child["PATH"] == _AMBIENT["PATH"]


def test_gh_token_and_dotenv_exported_credentials_are_scrubbed():
    """The child-env clause names GH_TOKEN and the ~/.no_human/.env contents
    explicitly. GH_TOKEN is TOKEN-shaped; the integration credentials
    config.load_env_var exports from .env into os.environ (an SSO password, a
    Jenkins/JIRA API token) are PASSWORD/TOKEN-shaped — so each is a foreign
    secret the child never sees, in both child-env shapes. Operational vars and
    each child's OWN billing credential are untouched; the other child's is not
    (the Codex shape drops CLAUDE_CODE_OAUTH_TOKEN)."""
    dotenv = {
        "GH_TOKEN": "gho_real",                 # the gh-CLI spelling of the token
        "SSO_PASSWORD": "hunter2",              # ~/.no_human/.env, exported by load_env_var
        "JENKINS_API_TOKEN": "jk_real",         # ~/.no_human/.env
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/op",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth_keep",  # the child's own credential
    }
    secrets = ("GH_TOKEN", "SSO_PASSWORD", "JENKINS_API_TOKEN")
    # Additive (Claude/SDK) shape: each foreign secret is blanked to "".
    env: dict[str, str] = {}
    blanked = scrub_foreign_secrets_into(env, dotenv)
    for name in secrets:
        assert env[name] == "" and name in blanked, name
    assert "PATH" not in env and "HOME" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # kept; child inherits the real value
    # Full-copy (Codex/subprocess) shape, with the keep-list codex_backend
    # really passes (the default, CODEX_CHILD_KEEP): each foreign secret is
    # deleted outright - and so is the Claude credential, which the Codex
    # child must never carry; the Codex child's own OPENAI_* survives.
    full = {**dotenv, "OPENAI_API_KEY": "sk_codex"}
    dropped = drop_foreign_secrets(full)
    for name in secrets + ("CLAUDE_CODE_OAUTH_TOKEN",):
        assert name not in full and name in dropped, name
    assert full["PATH"] == "/usr/bin:/bin"
    assert full["OPENAI_API_KEY"] == "sk_codex"


def test_additive_scrub_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOKEN", "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "keep")
    env: dict[str, str] = {}
    blanked = scrub_foreign_secrets_into(env)
    assert "SOME_VENDOR_TOKEN" in blanked and env["SOME_VENDOR_TOKEN"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_full_env_drop_deletes_foreign_secrets_and_keeps_the_rest():
    env = dict(_AMBIENT)
    dropped = drop_foreign_secrets(env)  # Codex keep-list
    assert sorted(dropped) == sorted([
        "GITHUB_TOKEN", "gitlab_token", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
        "SSH_AUTH_SOCK", "GOOGLE_APPLICATION_CREDENTIALS", "JIRA_API_TOKEN",
        "MY_VENDOR_APIKEY", "CLAUDE_CODE_OAUTH_TOKEN", "anthropic_api_key",
    ])
    assert env == {
        "PATH": "/usr/bin:/bin", "HOME": "/home/op", "GIT_ASKPASS": "/usr/bin/true",
        "NO_HUMAN_AGENT_SESSION_KEY": "1", "OPENAI_API_KEY": "sk_openai",
    }


def test_positive_control_a_secret_free_env_is_left_alone():
    env = {"PATH": "/bin"}
    assert scrub_foreign_secrets_into({}, env) == []
    assert drop_foreign_secrets(env) == []
    assert env == {"PATH": "/bin"}


def test_connection_urls_credential_files_and_key_suffixes_are_secrets():
    for name in ("DATABASE_URL", "REDIS_URL", "MONGODB_URI", "SENTRY_DSN", "NETRC",
                 "PGPASSFILE", "KUBECONFIG", "DOCKER_AUTH_CONFIG", "STRIPE_KEY",
                 "SIGNING_KEY", "ENCRYPTION_KEY", "COOKIE_KEY", "SLACK_WEBHOOK_URL",
                 "NPM_TOKEN", "CARGO_REGISTRY_TOKEN", "MAVEN_PASSWORD"):
        assert is_secret_env_name(name), name


def test_operational_names_a_task_suite_needs_are_not_secrets():
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PAGER", "AWS_CA_BUNDLE",
                 "AWS_ENDPOINT_URL", "TOKENIZERS_PARALLELISM", "PATH", "HOME",
                 "GIT_TERMINAL_PROMPT", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        assert not is_secret_env_name(name), name
    assert is_secret_env_name("AWS_PROFILE")  # the pointer to a credential file stays scrubbed


def test_a_connection_url_with_a_password_is_a_secret_under_any_name():
    # the class, not the six named instances
    for name in ("POSTGRES_URL", "MYSQL_URL", "AMQP_URL", "CELERY_BROKER_URL", "DATABASE_URI"):
        assert is_foreign_secret(name, CLAUDE_CHILD_KEEP, "postgres://app:s3cret@db.internal:5432/x"), name
    # no password in the userinfo, or no userinfo at all: an ordinary URL
    for value in ("https://api.example.com/v1", "postgres://db.internal/x", "postgres://app@db/x", ""):
        assert not is_foreign_secret("SERVICE_URL", CLAUDE_CHILD_KEEP, value), value
    env = {"CELERY_BROKER_URL": "amqp://guest:guest@rabbit/", "API_URL": "https://x", "PATH": "/bin"}
    assert drop_foreign_secrets(env) == ["CELERY_BROKER_URL"]
    assert scrub_foreign_secrets_into({}, {"POSTGRES_URL": "postgres://a:b@h/d"}) == ["POSTGRES_URL"]


def test_localstack_endpoint_family_and_key_suffix_edges():
    for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL_DYNAMODB"):
        assert not is_secret_env_name(name), name
    assert is_secret_env_name("DOCKER_CONFIG")
    # the `_KEY` suffix rule is deliberately broad: these non-secrets go too,
    # and docs/security.md says so
    for name in ("SORT_KEY", "PARTITION_KEY", "LICENSE_KEY", "SSH_PUBLIC_KEY"):
        assert is_secret_env_name(name), name


def test_an_authenticated_proxy_survives_into_both_children():
    """The child IS the process that reaches the model, and on a corporate
    network it does so through HTTPS_PROXY — a URL that carries a password.
    Stripping it would silently cut every coder/reviewer/planner session off
    from its model (an empty HTTPS_PROXY means "connect directly"). Same
    category as the child's own billing credential: kept, and documented."""
    proxy = {"HTTPS_PROXY": "http://corpuser:corppw@proxy.corp:8080",
             "http_proxy": "http://corpuser:corppw@proxy.corp:8080",
             "ALL_PROXY": "socks5://u:p@proxy.corp:1080", "NO_PROXY": "localhost",
             "PATH": "/bin", "GITHUB_TOKEN": "ghp_x"}
    assert scrub_foreign_secrets_into({}, proxy) == ["GITHUB_TOKEN"]
    env = dict(proxy)
    assert drop_foreign_secrets(env) == ["GITHUB_TOKEN"]
    assert env["HTTPS_PROXY"] == proxy["HTTPS_PROXY"] and env["http_proxy"] == proxy["http_proxy"]
    # positive control: the same credentialed URL under a non-proxy name goes
    assert drop_foreign_secrets({"UPSTREAM_URL": "http://corpuser:corppw@proxy.corp:8080"}) == ["UPSTREAM_URL"]
