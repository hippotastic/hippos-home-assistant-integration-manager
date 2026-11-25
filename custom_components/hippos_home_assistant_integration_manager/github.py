"""Read repository metadata and commit archives from GitHub."""

import asyncio
from contextlib import asynccontextmanager
import json
import re
from typing import Any
from urllib.parse import quote

from aiohttp import ClientError, ClientResponse, ClientSession
from yarl import URL

from .const import SHA_PATTERN, VERSION_PATTERN
from .models import ManagerError, Revision

_REPOSITORY = re.compile(
    r"(?:https://github\.com/)?([a-z\d](?:[a-z\d-]{0,38}))/([a-z\d_.-]{1,100})/?",
    re.ASCII | re.IGNORECASE,
)
_TOKEN = re.compile(r"github_pat_[a-zA-Z0-9_]{20,245}")
_BRANCH = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.\-/]{0,127}", re.ASCII)
_METADATA_LIMIT = 2 * 1024 * 1024
_ARCHIVE_LIMIT = 32 * 1024 * 1024
_API = URL("https://api.github.com")


def normalize_repository(value: str) -> str:
    """Accept a GitHub slug or an HTTPS GitHub repository URL."""
    match = _REPOSITORY.fullmatch(value.strip())
    if match is None or match[2] in {".", ".."}:
        raise ManagerError("invalid_repository")
    return f"{match[1]}/{match[2]}".lower()


def normalize_branch(value: str) -> str:
    """Accept an optional branch without Git revision expressions."""
    branch = value.strip()
    if branch and (
        _BRANCH.fullmatch(branch) is None
        or ".." in branch
        or any(
            not part or part.startswith(".") or part.endswith((".", ".lock"))
            for part in branch.split("/")
        )
    ):
        raise ManagerError("invalid_branch")
    return branch


class GitHub:
    """Use a repository-scoped fine-grained token for GitHub API requests."""

    def __init__(self, session: ClientSession, repository: str, token: str) -> None:
        """Set up a client whose authorization header has a fixed origin."""
        self.repository = normalize_repository(repository)
        if _TOKEN.fullmatch(token.strip()) is None:
            raise ManagerError("invalid_token")
        self._session = session
        self._token = token.strip()
        self._api_path = f"/repos/{self.repository}"
        self._web_url = f"https://github.com/{self.repository}"

    @asynccontextmanager
    async def _request(self, url: URL, *, authenticated: bool):
        headers = {"Accept-Encoding": "identity"}
        if authenticated:
            headers.update(
                {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )

        # Keep request URLs, redirect tickets and response bodies out of HA errors.
        try:
            async with asyncio.timeout(45), self._session.get(
                url, headers=headers, allow_redirects=False, auto_decompress=False
            ) as response:
                yield response
        except (ClientError, TimeoutError, ValueError):
            raise ManagerError("cannot_connect") from None

    @staticmethod
    def _require_success(response: ClientResponse, *, authenticated: bool) -> None:
        if response.status == 200:
            return
        if authenticated and response.status == 401:
            raise ManagerError("invalid_token")
        if response.status == 429 or (
            response.status == 403
            and (
                response.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in response.headers
            )
        ):
            raise ManagerError("rate_limited")
        if authenticated and response.status in (403, 404):
            raise ManagerError("repository_inaccessible")
        raise ManagerError("cannot_connect")

    @staticmethod
    async def _body(response: ClientResponse, limit: int) -> bytes:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ManagerError("invalid_response")
        if response.content_length is not None and response.content_length > limit:
            raise ManagerError("download_too_large")
        contents = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            contents.extend(chunk)
            if len(contents) > limit:
                raise ManagerError("download_too_large")
        return bytes(contents)

    async def _json(self, suffix: str, *, missing: str | None = None) -> dict[str, Any]:
        url = URL(f"{_API}{self._api_path}{suffix}", encoded=True)
        async with self._request(url, authenticated=True) as response:
            if response.status == 404 and missing is not None:
                raise ManagerError(missing)
            self._require_success(response, authenticated=True)
            try:
                value = json.loads(await self._body(response, _METADATA_LIMIT))
            except (ValueError, RecursionError):
                raise ManagerError("invalid_response") from None
        if not isinstance(value, dict):
            raise ManagerError("invalid_response")
        return value

    async def repository_id(self) -> int:
        """Read the stable repository identity and check the configured name."""
        metadata = await self._json("")
        full_name = metadata.get("full_name")
        repo_id = metadata.get("id")
        if (
            not isinstance(full_name, str)
            or full_name.lower() != self.repository
            or type(repo_id) is not int
            or repo_id <= 0
        ):
            raise ManagerError("invalid_response")
        return repo_id

    async def latest(self, branch: str) -> Revision:
        """Find a published stable release or the head of an explicitly chosen branch."""
        branch = normalize_branch(branch)
        if branch:
            branch_info = await self._json(
                f"/branches/{quote(branch, safe='')}", missing="branch_missing"
            )
            commit = branch_info.get("commit")
            sha = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None:
                raise ManagerError("invalid_response")
            return Revision(sha, f"{branch}@{sha[:12]}", f"{self._web_url}/commit/{sha}", False)

        release = await self._json("/releases/latest", missing="release_missing")
        tag = release.get("tag_name")
        if (
            not isinstance(tag, str)
            or VERSION_PATTERN.fullmatch(tag.removeprefix("v")) is None
            or release.get("draft") is not False
            or release.get("prerelease") is not False
        ):
            raise ManagerError("invalid_release")
        # Resolve the release tag explicitly, including annotated tags.
        ref = await self._json(f"/git/ref/tags/{tag}")
        for _ in range(5):
            target = ref.get("object")
            sha = target.get("sha") if isinstance(target, dict) else None
            if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None:
                break
            if target.get("type") == "commit":
                return Revision(
                    sha, tag.removeprefix("v"), f"{self._web_url}/releases/tag/{tag}", True
                )
            if target.get("type") != "tag":
                break
            ref = await self._json(f"/git/tags/{sha}")
        raise ManagerError("invalid_release")

    async def archive(self, sha: str) -> bytes:
        """Download the exact commit through GitHub's short-lived codeload URL."""
        if SHA_PATTERN.fullmatch(sha) is None:
            raise ManagerError("invalid_response")
        url = _API.with_path(f"{self._api_path}/zipball/{sha}")
        async with self._request(url, authenticated=True) as response:
            if response.status == 200:
                return await self._body(response, _ARCHIVE_LIMIT)
            if response.status != 302:
                self._require_success(response, authenticated=True)
            download_url = URL(response.headers.get("Location", ""), encoded=True)
            if (
                download_url.scheme != "https"
                or download_url.raw_host != "codeload.github.com"
                or download_url.port != 443
                or download_url.raw_user is not None
                or download_url.raw_password is not None
                or download_url.raw_fragment
            ):
                raise ManagerError("invalid_download_redirect")

        async with self._request(download_url, authenticated=False) as response:
            self._require_success(response, authenticated=False)
            return await self._body(response, _ARCHIVE_LIMIT)
