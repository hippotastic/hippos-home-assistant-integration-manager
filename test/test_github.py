"""Exercise GitHub routing, pinning and response validation without a network."""

from contextlib import asynccontextmanager
import json
from unittest import IsolatedAsyncioTestCase

from yarl import URL

from custom_components.hippos_home_assistant_integration_manager.github import GitHub, normalize_branch
from custom_components.hippos_home_assistant_integration_manager.models import ManagerError

from fixtures import FIRST, REPOSITORY, REPOSITORY_ID, TOKEN


class Reply:
    def __init__(self, status, *, payload=None, raw=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.raw = json.dumps(payload).encode() if payload is not None else raw
        self.content_length = len(self.raw)
        self.content = self

    async def iter_chunked(self, size):
        yield self.raw


class Session:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    @asynccontextmanager
    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        yield next(self.replies)


class GitHubTests(IsolatedAsyncioTestCase):
    async def test_release_tag_resolves_annotated_tag_to_commit(self):
        tag_sha = "c" * 40
        session = Session(
            Reply(200, payload={"full_name": REPOSITORY, "id": REPOSITORY_ID}),
            Reply(200, payload={"tag_name": "v1.0.0", "draft": False, "prerelease": False}),
            Reply(200, payload={"object": {"type": "tag", "sha": tag_sha}}),
            Reply(200, payload={"object": {"type": "commit", "sha": FIRST.sha}}),
        )
        github = GitHub(session, f"https://github.com/{REPOSITORY.upper()}/", TOKEN)
        self.assertEqual(await github.repository_id(), REPOSITORY_ID)
        self.assertEqual(await github.latest(""), FIRST)
        self.assertEqual(session.calls[2][0].path, f"/repos/{REPOSITORY}/git/ref/tags/v1.0.0")
        for url, options in session.calls:
            self.assertEqual(url.host, "api.github.com")
            self.assertEqual(options["headers"]["Authorization"], f"Bearer {TOKEN}")
            self.assertFalse(options["allow_redirects"])

    async def test_archive_redirect_never_receives_pat(self):
        redirect = f"https://codeload.github.com/{REPOSITORY}/legacy.zip/{FIRST.sha}?token=temporary"
        session = Session(Reply(302, headers={"Location": redirect}), Reply(200, raw=b"zip"))
        self.assertEqual(await GitHub(session, REPOSITORY, TOKEN).archive(FIRST.sha), b"zip")
        self.assertEqual(session.calls[1][0], URL(redirect))
        self.assertNotIn("Authorization", session.calls[1][1]["headers"])
        self.assertFalse(session.calls[1][1]["allow_redirects"])

    async def test_unexpected_redirects_are_rejected_without_disclosing_location(self):
        for redirect in (
            "http://codeload.github.com/thing?token=temporary",
            "https://codeload.github.com.evil.invalid/thing?token=temporary",
            "https://some-user@codeload.github.com/thing?token=temporary",
            "https://codeload.github.com:444/thing?token=temporary",
        ):
            with self.subTest(redirect=redirect):
                session = Session(Reply(302, headers={"Location": redirect}))
                with self.assertRaises(ManagerError) as error:
                    await GitHub(session, REPOSITORY, TOKEN).archive(FIRST.sha)
                self.assertEqual(error.exception.key, "invalid_download_redirect")
                self.assertNotIn("temporary", str(error.exception))
                self.assertEqual(len(session.calls), 1)

    async def test_access_errors_are_distinguished_from_rate_limits(self):
        for reply, error_key in (
            (Reply(401, raw=TOKEN.encode()), "invalid_token"),
            (Reply(404), "repository_inaccessible"),
            (Reply(403, headers={"X-RateLimit-Remaining": "0"}), "rate_limited"),
        ):
            with self.subTest(error_key=error_key), self.assertRaises(ManagerError) as error:
                await GitHub(Session(reply), REPOSITORY, TOKEN).repository_id()
            self.assertEqual(error.exception.key, error_key)
            self.assertNotIn(TOKEN, str(error.exception))

    async def test_branch_slashes_are_encoded_as_part_of_the_ref(self):
        session = Session(Reply(200, payload={"commit": {"sha": FIRST.sha}}))
        revision = await GitHub(session, REPOSITORY, TOKEN).latest("feature/nightly")
        self.assertFalse(revision.stable)
        self.assertEqual(revision.sha, FIRST.sha)
        self.assertTrue(session.calls[0][0].raw_path.endswith("/branches/feature%2Fnightly"))
        for branch in ("main~1", "foo/../main", "main@{0}", "bad//ref"):
            with self.subTest(branch=branch), self.assertRaises(ManagerError):
                normalize_branch(branch)
