"""HTTP layer: auth, the git relay contract, GitHub-style browsing."""

import base64
import gzip

import httpx
import pytest

from app.main import raw_media_type, split_ref_path


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- auth ---------------------------------------------------------------------


def test_health_is_public(api):
    del api.headers["X-API-Key"]
    assert api.get("/health").json()["data"] == {"status": "up", "token_configured": True}


def test_missing_key_gets_basic_challenge(api):
    # git only sends the user:key from the clone URL after this challenge.
    del api.headers["X-API-Key"]
    response = api.get("/me/repo/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": basic("x", "test-key")},
        {"Authorization": basic("test-key", "")},
        {"Authorization": "Bearer test-key"},
    ],
)
def test_key_accepted_from_basic_or_bearer(api, upstream, headers):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(200, json={"full_name": "me/repo"})
    del api.headers["X-API-Key"]
    assert api.get("/me/repo", headers=headers).status_code == 200


def test_wrong_key_is_rejected_before_upstream(api, upstream):
    response = api.get("/me/repo", headers={"X-API-Key": "nope", "Authorization": basic("x", "nope")})
    assert response.status_code == 401
    assert upstream.requests == []


def test_invalid_repo_name_never_reaches_upstream(api, upstream):
    assert api.get("/me/re%20po").status_code == 422
    assert upstream.requests == []


# --- git relay ------------------------------------------------------------------


def test_info_refs_relays_protocol_v2_and_swaps_auth(api, upstream):
    advert = b"001e# service=git-upload-pack\n0000"
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(
        200, content=advert, headers={"Content-Type": "application/x-git-upload-pack-advertisement"}
    )
    response = api.get(
        "/me/repo.git/info/refs",
        params={"service": "git-upload-pack"},
        headers={"Git-Protocol": "version=2", "Authorization": basic("x", "test-key")},
    )
    assert response.status_code == 200 and response.content == advert
    assert response.headers["content-type"] == "application/x-git-upload-pack-advertisement"
    sent, _ = upstream.requests[0]
    assert sent.url.params["service"] == "git-upload-pack"
    assert sent.headers["git-protocol"] == "version=2"
    # The caller's credentials are replaced by the GitHub token, never forwarded.
    assert sent.headers["authorization"] == basic("x-access-token", "ghp_test")


def test_upload_pack_relays_gzip_body_and_response_untouched(api, upstream):
    request_body = gzip.compress(b"0032want 0123456789012345678901234567890123456789\n0000")
    pack = gzip.compress(b"PACK" + b"\x00" * 64)

    def handler(request, body):
        assert body == request_body
        return httpx.Response(
            200, content=pack, headers={"Content-Type": "application/x-git-upload-pack-result", "Content-Encoding": "gzip"}
        )

    upstream.routes[("POST", "/me/repo.git/git-upload-pack")] = handler
    response = api.post(
        "/me/repo/git-upload-pack",
        content=request_body,
        headers={
            "Content-Type": "application/x-git-upload-pack-request",
            "Content-Encoding": "gzip",
            "Accept-Encoding": "deflate",
        },
    )
    assert response.status_code == 200
    sent, _ = upstream.requests[0]
    assert sent.headers["content-encoding"] == "gzip"
    # Upstream is asked for git's own encodings, not httpx's defaults.
    assert sent.headers["accept-encoding"] == "deflate"
    assert response.headers["content-encoding"] == "gzip"


def test_receive_pack_is_relayed(api, upstream):
    upstream.routes[("POST", "/me/repo.git/git-receive-pack")] = httpx.Response(200, content=b"000eunpack ok\n0000")
    response = api.post("/me/repo.git/git-receive-pack", content=b"push-data")
    assert response.status_code == 200
    assert upstream.requests[0][1] == b"push-data"


def test_unknown_git_service_is_rejected(api, upstream):
    assert api.get("/me/repo/info/refs", params={"service": "git-upload-archive"}).status_code == 422
    assert api.post("/me/repo/git-upload-archive", content=b"x").status_code == 422
    assert upstream.requests == []


def test_upstream_401_never_reaches_git_as_401(api, upstream):
    # A 401 would make git blame our API key; GitHub sends one for a revoked token.
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(401, headers={"WWW-Authenticate": "Basic"})
    response = api.get("/me/repo/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers
    assert "GITHUB_TOKEN" in response.text


def test_push_without_token_says_so(api, upstream, settings):
    settings.github_token = ""
    upstream.routes[("GET", "/me/repo.git/info/refs")] = httpx.Response(401)
    response = api.get("/me/repo/info/refs", params={"service": "git-receive-pack"})
    assert response.status_code == 403 and "Pushing needs GITHUB_TOKEN" in response.text


def test_missing_repo_is_404(api, upstream):
    response = api.get("/me/ghost/info/refs", params={"service": "git-upload-pack"})
    assert response.status_code == 404 and "not found" in response.text


# --- browsing ---------------------------------------------------------------------


def test_repo_info_gives_clone_url_on_this_host(api, upstream):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(
        200, json={"full_name": "me/repo", "private": True, "default_branch": "main", "permissions": {"push": True}}
    )
    data = api.get("/me/repo.git").json()["data"]
    assert data["clone_url"] == "http://testserver/me/repo.git"
    assert data["private"] is True and data["can_push"] is True


def test_blob_streams_raw_content_as_plain_text(api, upstream):
    def handler(request, _):
        assert request.headers["accept"] == "application/vnd.github.raw"
        assert request.url.params["ref"] == "dev"
        return httpx.Response(200, content=b"<script>alert(1)</script>")

    upstream.routes[("GET", "/repos/me/repo/contents/web/index.html")] = handler
    response = api.get("/me/repo/blob/dev/web/index.html")
    assert response.text == "<script>alert(1)</script>"
    # Never rendered as HTML on this domain.
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    # Behind Cloudflare, anything cacheable would be served to callers without the key.
    assert response.headers["cache-control"] == "private, no-store"


def test_blob_ref_with_slash_goes_in_query(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/src/a.py")] = lambda request, _: httpx.Response(
        200, content=request.url.params["ref"].encode()
    )
    assert api.get("/me/repo/blob/src/a.py", params={"ref": "feature/x"}).text == "feature/x"


def test_tree_lists_dirs_first(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/contents/src")] = httpx.Response(
        200,
        json=[
            {"name": "b.py", "type": "file", "size": 3},
            {"name": "lib", "type": "dir", "size": 0},
            {"name": "A.py", "type": "file", "size": 1},
        ],
    )
    data = api.get("/me/repo/tree/main/src").json()["data"]
    assert [entry["name"] for entry in data["entries"]] == ["lib", "A.py", "b.py"]
    assert data["ref"] == "main" and data["path"] == "src"


def test_recursive_tree_filters_on_path_and_caps(api, upstream, settings):
    settings.gh_max_tree_entries = 2
    upstream.routes[("GET", "/repos/me/repo/git/trees/HEAD")] = httpx.Response(
        200,
        json={
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "size": 1},
                {"path": "src", "type": "tree"},
                {"path": "src/a.py", "type": "blob", "size": 2},
                {"path": "src/b.py", "type": "blob", "size": 2},
                {"path": "src/c.py", "type": "blob", "size": 2},
                {"path": "vendor", "type": "commit"},
            ],
        },
    )
    data = api.get("/me/repo/tree", params={"recursive": True}).json()["data"]
    assert data["count"] == 2 and data["truncated"] is True
    upstream.routes[("GET", "/repos/me/repo/git/trees/main")] = upstream.routes[("GET", "/repos/me/repo/git/trees/HEAD")]
    settings.gh_max_tree_entries = 100
    data = api.get("/me/repo/tree/main/src", params={"recursive": True}).json()["data"]
    assert [entry["path"] for entry in data["entries"]] == ["src/a.py", "src/b.py", "src/c.py"]


def test_archive_follows_codeload_redirect_without_leaking_it(api, upstream):
    upstream.routes[("GET", "/repos/me/repo/tarball/main")] = httpx.Response(
        302, headers={"Location": "https://codeload.test/me/repo/tar.gz/main?token=SECRET"}
    )
    upstream.routes[("GET", "/me/repo/tar.gz/main")] = httpx.Response(200, content=b"tarbytes")
    response = api.get("/me/repo/archive/main.tar.gz")
    assert response.content == b"tarbytes"
    assert "SECRET" not in str(response.headers)
    assert 'filename="repo-main.tar.gz"' in response.headers["content-disposition"]


def test_rate_limit_is_reported(api, upstream):
    upstream.routes[("GET", "/repos/me/repo")] = httpx.Response(403, headers={"x-ratelimit-remaining": "0"})
    response = api.get("/me/repo")
    assert response.status_code == 429 and response.json()["error_code"] == "rate_limited"


def test_path_traversal_is_rejected(api, upstream):
    assert api.get("/me/repo/blob/main/a/%2E%2E/b").status_code == 422
    assert upstream.requests == []


# --- helpers ------------------------------------------------------------------------


def test_split_ref_path():
    assert split_ref_path("main/src/app.py", None) == ("main", "src/app.py")
    assert split_ref_path("main", None) == ("main", "")
    assert split_ref_path("", None) == (None, "")
    assert split_ref_path("src/app.py", "feature/x") == ("feature/x", "src/app.py")


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.py", "text/plain; charset=utf-8"),
        ("Makefile", "text/plain; charset=utf-8"),
        ("logo.svg", "text/plain; charset=utf-8"),
        ("page.html", "text/plain; charset=utf-8"),
        ("data.json", "text/plain; charset=utf-8"),
        ("photo.png", "image/png"),
        ("doc.pdf", "application/pdf"),
    ],
)
def test_raw_media_type(path, expected):
    assert raw_media_type(path) == expected
