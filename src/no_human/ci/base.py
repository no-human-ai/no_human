"""CI data types + the pluggable backend interface.

The orchestrator depends only on ``CIBackend.trigger`` — never on ``glab`` or any
provider CLI directly. ``ci_from_config`` selects the concrete backend from the
project profile / config, so adding GitHub Actions or Jenkins is a new subclass,
not a change to the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class HumanGatedCI(Exception):
    """Raised by a backend whose pipeline must be started by a human (e.g. a
    Jenkins image build). The orchestrator parks the task with a wake condition
    rather than faking or skipping the step."""

    def __init__(self, message: str, wake_hint: str = "",
                 kind: str = "", name: str = ""):
        super().__init__(message)
        self.wake_hint = wake_hint
        self.kind = kind
        self.name = name

    def to_dict(self) -> dict[str, str]:
        return {"message": str(self), "wake_hint": self.wake_hint,
                "kind": self.kind, "name": self.name}


class CIMisconfigured(ValueError):
    """``ci.enabled`` is true but the block cannot produce a backend.

    RAISED, never returned, and that is the whole point. There are two ways to
    end up with no CI backend and they are not the same answer:

    * ``ci.enabled: false`` — the operator declined CI. Supported, silent,
      cheap; the caller proceeds on the local suite. Still a bare ``None``.
    * ``ci.enabled: true`` with no pipeline target — the operator asked for a
      gate and does not have one.

    ``ci_from_config`` used to answer both with ``None``, so no caller could
    tell them apart, and the reading every caller defaulted to was the silent
    one: PRs opened ungated while the user believed the advertised gate ran
    (KNOWN_ISSUES KI-5). A sentinel or a second return value would have fixed
    the *ambiguity* while leaving the *default* wrong — both require every
    caller, forever, to remember a check, and the cost of forgetting is
    invisible. An exception cannot be defaulted past: a caller that does not
    handle it stops. That asymmetry is the design.

    Subclasses ``ValueError`` because the unknown-backend branch has always
    raised one and callers guard on it — widening what is raised must never
    narrow what is caught.

    ``backend`` and ``missing`` are carried structurally, not only in the
    message, so an escalation can name the exact key without re-parsing prose.
    """

    def __init__(self, message: str, *, backend: str = "",
                 missing: tuple[str, ...] = ()):
        super().__init__(message)
        self.backend = backend
        self.missing = tuple(missing)


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    MANUAL = "manual"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in (
            PipelineStatus.SUCCESS,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELED,
            PipelineStatus.SKIPPED,
        )

    @property
    def is_success(self) -> bool:
        return self == PipelineStatus.SUCCESS


@dataclass
class JobResult:
    name: str
    status: str
    failure_reason: str | None = None
    web_url: str = ""
    # True only for a result built from a test REPORT case (Jenkins'
    # `_failing_tests`), whose `name` is a test id. Every other backend reports
    # job/check/status names; `_ci_failure_unrelated` never calls such a
    # failure "unrelated" to the diff (#429).
    is_test_case: bool = False


@dataclass
class CIResult:
    pipeline_id: str
    pipeline_url: str
    status: PipelineStatus
    jobs: list[JobResult] = field(default_factory=list)
    infra_failure: bool = False
    # An access/permission wall (401/403, missing token, forbidden) — distinct
    # from a transient infra failure: retrying won't help, only a human granting
    # access will. The orchestrator routes this to a MISSING_ACCESS blocker.
    access_failure: bool = False
    # The exact ~/.no_human/.env key the human must set to clear an access wall
    # (e.g. "JENKINS_API_TOKEN"), so the escalation names it precisely (WS-F).
    access_env_key: str = ""
    parsed_output: str = ""

    @property
    def passed(self) -> bool:
        return self.status == PipelineStatus.SUCCESS

    @property
    def failed(self) -> bool:
        return self.status in (PipelineStatus.FAILED, PipelineStatus.CANCELED)

    @property
    def summary(self) -> str:
        if self.access_failure:
            verdict = "ACCESS-DENIED"
        elif self.infra_failure:
            verdict = "INFRA-FAIL"
        elif self.passed:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        pid = self.pipeline_id or "?"
        return f"[{verdict}] pipeline #{pid}: {self.status.value}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "pipeline_url": self.pipeline_url,
            "status": self.status.value,
            "infra_failure": self.infra_failure,
            "access_failure": self.access_failure,
            "passed": self.passed,
            "jobs": [{"name": j.name, "status": j.status,
                      "failure_reason": j.failure_reason} for j in self.jobs],
        }


class CIBackend(ABC):
    """Provider-agnostic CI contract the orchestrator drives.

    Implementations: ``GitLabCI`` (complete), ``GitHubActionsCI`` and
    ``JenkinsCI`` (seams). All must retry only on infra failures, never on real
    test failures, and surface a parsed ``CIResult``.
    """

    name: str = "ci"
    # Backends that don't auto-retry infra (seams, human-gated) inherit 0 so the
    # orchestrator can read it unconditionally without crashing.
    max_infra_retries: int = 0

    @abstractmethod
    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """Trigger CI for ``branch``, wait for a terminal status, return the
        parsed result. Raise ``HumanGatedCI`` if a human must start the run."""
        raise NotImplementedError

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking poll of an existing pipeline. Returns immediately with
        the current status. Subclasses should override; the default returns
        UNKNOWN (safe fallback — WakeWatcher will re-check next tick)."""
        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url="",
            status=PipelineStatus.UNKNOWN,
        )
