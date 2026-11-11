"""Public release checks must work when the anonymous GitHub API is throttled."""
import io
import json
import sys
import unittest
import urllib.error
from urllib.parse import urlsplit
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class Response(io.BytesIO):
    def __init__(self, body, url="", headers=None):
        super().__init__(body)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class ReleaseCheckTest(unittest.TestCase):
    def test_rate_limited_api_uses_public_release_and_download_links(self):
        base = "https://github.com/" + server.GITHUB_CODE_REPO
        tag = base + "/releases/tag/v0.5.0"
        download = base + "/releases/download/v0.5.0/quotes.app.zip"

        def network(req, timeout):
            url = req.full_url
            if url.startswith("https://api.github.com/"):
                raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)
            if urlsplit(url)._replace(query="").geturl() == base + "/releases/latest":
                return Response(b"", tag)
            if url == base + "/releases/expanded_assets/v0.5.0":
                return Response(('<a href="' + download + '">quotes.app.zip</a>').encode())
            self.fail("Unexpected network request: " + url)

        with patch.object(server, "read_gh_token", return_value=""), patch("urllib.request.urlopen", network):
            try:
                release = server.fetch_latest_release()
            except Exception as exc:
                self.fail("A public release should remain discoverable: " + str(exc))
        self.assertEqual(release["tag_name"], "v0.5.0")
        self.assertEqual(release["html_url"], tag)
        self.assertEqual(release["assets"][0]["browser_download_url"], download)

    def test_shared_proxy_cache_cannot_hide_a_new_release(self):
        base = "https://github.com/" + server.GITHUB_CODE_REPO

        def network(req, timeout):
            url = req.full_url
            if url.startswith("https://api.github.com/"):
                raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)
            if urlsplit(url).path.endswith("/latest"):
                tag = "v0.5.1" if urlsplit(url).query else "v0.4.1"
                return Response(b"", base + "/releases/tag/" + tag)
            return Response(b"")

        with patch.object(server, "read_gh_token", return_value=""), patch("urllib.request.urlopen", network):
            self.assertEqual(server.fetch_latest_release()["tag_name"], "v0.5.1")

    def test_no_release_is_an_error_not_a_current_version(self):
        error = urllib.error.HTTPError("https://api.github.com", 404, "Not Found", {}, None)
        with patch.object(server, "read_gh_token", return_value=""), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                server.fetch_latest_release()

    def test_api_release_metadata_is_preserved(self):
        release = {"tag_name": "v0.5.0", "assets": [{"name": "quotes.app.zip", "url": "https://api.github.com/asset/1"}]}
        with patch.object(server, "read_gh_token", return_value=""), patch("urllib.request.urlopen", return_value=Response(json.dumps(release).encode())):
            self.assertEqual(server.fetch_latest_release(), release)

    def test_download_uses_public_url_without_sending_api_credentials(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "app.zip")

            def network(req, timeout):
                self.assertEqual(req.full_url, "https://github.com/example/app.zip")
                self.assertIsNone(req.get_header("Authorization"))
                return Response(b"PK-synthetic-zip", headers={"Content-Length": "16"})

            with patch.object(server, "read_gh_token", return_value="synthetic-secret"), patch("urllib.request.urlopen", network):
                server.download_asset("https://github.com/example/app.zip", target)
            self.assertEqual(Path(target).read_bytes(), b"PK-synthetic-zip")
