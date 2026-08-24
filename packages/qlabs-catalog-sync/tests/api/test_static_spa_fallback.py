"""The static-asset mount and SPA history fallback (``api/static.py``): an unknown path
under :data:`~qlabs_catalog_sync.api.app.API_PREFIX` always returns the JSON error
model, an unknown path outside it returns the SPA shell (when built assets exist) or a
clear explanation (when they do not) -- never a 500, never a silently-wrong body.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qlabs_catalog_sync.api.app import API_PREFIX

from .api_helpers import build_app, write_console_dist

_SPA_SENTINEL = "<html>SPA-SENTINEL-do-not-confuse-with-json</html>"


def test_root_with_no_static_dir_returns_clear_json_not_500_not_confusing_404() -> None:
    app = build_app(static_dir=None)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["code"] == "console_not_installed"
    assert isinstance(body["message"], str) and body["message"]


def test_root_with_static_dir_configured_but_no_index_html_is_also_clear_json(
    tmp_path: Path,
) -> None:
    empty_dist = tmp_path / "empty-dist"
    empty_dist.mkdir()

    app = build_app(static_dir=empty_dist)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 404
    assert response.json()["code"] == "console_not_installed"


def test_root_serves_index_html_when_console_assets_are_present(tmp_path: Path) -> None:
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == _SPA_SENTINEL


def test_unknown_client_side_route_falls_back_to_index_html(tmp_path: Path) -> None:
    """SPA history-mode routing: ``/endpoints/42`` has no matching file on disk, so the
    shell loads and the console's own router takes over client-side."""
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/endpoints/42")

    assert response.status_code == 200
    assert response.text == _SPA_SENTINEL


def test_real_asset_file_is_served_as_itself_not_the_shell(tmp_path: Path) -> None:
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.text == "console.log('hi');\n"
    assert response.text != _SPA_SENTINEL
    assert "javascript" in response.headers["content-type"]


def test_unknown_api_path_returns_json_error_even_when_console_assets_exist(tmp_path: Path) -> None:
    """THE DISHONEST-CASE TEST: this must fail if the SPA fallback is ever registered
    (or reordered) such that it shadows the API. With a real ``index.html`` present and
    containing a distinctive sentinel, a request under ``API_PREFIX`` for a path no route
    claims must still come back as the JSON error model -- never the sentinel HTML, and
    never a 200.
    """
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/definitely-not-a-real-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert _SPA_SENTINEL not in response.text
    body = response.json()
    assert body["code"] == "not_found"


def test_unknown_api_path_returns_json_error_when_no_console_assets_exist() -> None:
    app = build_app(static_dir=None)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"{API_PREFIX}/definitely-not-a-real-route")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    # Distinct from the "console not installed" case at "/" -- the client asked for an
    # API route, and the honest answer is "that route doesn't exist", not "no console".
    assert body["code"] != "console_not_installed"


def test_path_traversal_attempt_never_escapes_the_static_directory(tmp_path: Path) -> None:
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("top secret, must never be served", encoding="utf-8")

    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/../outside-secret.txt")

    # Starlette normalizes "/../x" to "/x" before routing even reaches our handler in
    # most cases; either way, the secret file's contents must never come back, and the
    # only acceptable non-error outcome is the SPA shell.
    assert "top secret" not in response.text
    if response.status_code == 200:
        assert response.text == _SPA_SENTINEL


def test_healthz_and_metrics_still_win_over_the_spa_fallback_when_assets_exist(
    tmp_path: Path,
) -> None:
    """Registration order proof: even with a real ``index.html`` present, ``/healthz``
    and ``/metrics`` are still answered by their own routes, not swallowed by the
    catch-all fallback that matches every path."""
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app, raise_server_exceptions=False)

    healthz = client.get("/healthz")
    metrics = client.get("/metrics")

    metrics_content_type = metrics.headers["content-type"]
    assert healthz.headers["content-type"] == "application/json"
    assert _SPA_SENTINEL not in healthz.text
    assert "text/plain" in metrics_content_type or "version=" in metrics_content_type
    assert _SPA_SENTINEL not in metrics.text


def test_the_spa_shell_is_never_served_from_cache_without_asking(tmp_path: Path) -> None:
    """``index.html`` is what decides which build a browser is running.

    Every JS and CSS file the bundler emits is content-hashed, so a deploy changes only
    *which* files ``index.html`` points at. Served with no ``Cache-Control``, a browser may
    invent a freshness lifetime from ``Last-Modified`` and skip revalidating -- and an
    operator who has just upgraded keeps getting the previous console, with nothing on screen
    to say so and no way to diagnose it short of knowing to hard-refresh. That is a deploy
    that silently did not take effect, which is why this is pinned rather than left to a
    browser default.
    """
    static_dir = write_console_dist(tmp_path, index_html=_SPA_SENTINEL)
    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    for path in ("/", "/endpoints"):
        response = client.get(path)

        assert response.status_code == 200
        assert response.text == _SPA_SENTINEL
        assert "no-cache" in response.headers.get("cache-control", ""), (
            f"{path} lets a browser keep a stale console shell without revalidating"
        )


def test_a_content_hashed_asset_is_cached_indefinitely(tmp_path: Path) -> None:
    """The other half, and the reason the shell can afford to revalidate every load: a
    hashed file's name changes when its bytes do, so a stale copy is unreachable rather than
    merely old. Without this the shell's ``no-cache`` would turn every page load into a full
    re-download of the bundle."""
    static_dir = write_console_dist(tmp_path)
    app = build_app(static_dir=static_dir)
    client = TestClient(app)

    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control
