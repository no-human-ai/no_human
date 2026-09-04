"""GET /api/version — the board's only source of truth for the running version
when it is served in a plain browser.

Inside the desktop shell the version arrives over the preload bridge as
``window.nhDesktop.version``. In a browser there is no bridge, so Settings >
Updates rendered "You are running no_human unknown in a browser". The server is
the installed package, so it can simply say which one it is.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import no_human
from no_human.api.app import app
from no_human.config import Config


@pytest_asyncio.fixture
async def client(store, tmp_path):
    app.state.store = store
    app.state.config = Config(data={}, path=tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_version_is_the_running_package_version(client):
    r = await client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == no_human.__version__


@pytest.mark.asyncio
async def test_version_is_a_real_version_string_not_a_placeholder(client):
    """The whole point is to stop printing "unknown"; serving another
    placeholder would just move the defect."""
    version = (await client.get("/api/version")).json()["version"]
    assert isinstance(version, str) and version
    assert version.lower() not in {"unknown", "none", "null", ""}
    assert version[0].isdigit(), f"expected a version number, got {version!r}"


@pytest.mark.asyncio
async def test_version_carries_only_version_and_channel_fields(client):
    """It is deliberately NOT part of /api/config: that payload is already
    broader than it should be, and a version is not configuration. This pins the
    shape so nothing else gets attached to it later."""
    body = (await client.get("/api/version")).json()
    assert set(body) == {"version", "dist_name", "published"}


@pytest.mark.asyncio
async def test_version_carries_the_real_distribution_channel(client):
    """The browser Updates panel derives its upgrade instruction from these
    fields rather than a hardcoded package name — pin the values it depends
    on."""
    body = (await client.get("/api/version")).json()
    assert body["dist_name"] == "no-human"
    assert isinstance(body["published"], bool)


@pytest.mark.asyncio
async def test_version_needs_no_config_or_store(tmp_path):
    """Settings fetches this on open; it must not depend on request state that
    a partially-initialised app might not have."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        r = await c.get("/api/version")
    assert r.status_code == 200
    assert r.json()["version"] == no_human.__version__


def test_the_declared_version_and_the_installed_metadata_agree():
    """`no_human.__version__` and the distribution's own metadata are two
    independent literals — `src/no_human/__init__.py` and `pyproject.toml`'s
    `version` — and a release that bumps one and forgets the other is silent.

    It was not caught by anything when it happened: the wheel's metadata said
    0.1.1 while the program said 0.1.0, so `nh --version` and `GET /api/version`
    reported the old number, and `updates.check_for_update` compared the OLD
    version against the NEW published one and told every user of the new
    release that an upgrade was available — a banner upgrading could never
    clear. `core/build_info._dist_version()` reads the metadata, so the two
    disagreed inside one process.

    Skipped when the distribution is not installed (a bare source checkout
    running pytest without `uv sync`), because there is no metadata to compare
    against and asserting would fail for a reason that is not this defect.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = version("no-human")
    except PackageNotFoundError:  # pragma: no cover - only in a bare checkout
        import pytest as _pytest

        _pytest.skip("no-human is not installed in this environment")
    assert installed == no_human.__version__, (
        f"pyproject/dist metadata says {installed!r} but no_human.__version__ is "
        f"{no_human.__version__!r} — bump both, or `nh --version` and the update "
        f"check will disagree with the wheel"
    )


@pytest.mark.asyncio
async def test_the_openapi_document_reports_the_running_version(client):
    """`/openapi.json` and `/docs` are a THIRD place a version is published, and
    it used to be a hardcoded `0.1.0` in the `FastAPI(...)` call — it stayed
    0.1.0 through a release that moved `__version__` and `pyproject.toml`, so
    any tool reading the schema saw a version the build had left behind. The
    app now reads the package's own version; this pins that it keeps doing so.
    """
    from no_human.api.app import app as _app

    assert _app.openapi()["info"]["version"] == no_human.__version__
