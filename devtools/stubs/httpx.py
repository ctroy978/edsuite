"""Minimal httpx stub for offline cleanup testing."""

from __future__ import annotations

class Timeout:
    def __init__(self, timeout: float, read: float | None = None) -> None:
        self.timeout = timeout
        self.read = read


class Response:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"Mock HTTP status {self.status_code}")


def _extract_text(messages: list[dict]) -> str:
    if len(messages) < 2:
        return ""
    content = str(messages[1].get("content", ""))
    if "<BEGIN_TEXT>" in content and "<END_TEXT>" in content:
        snippet = content.split("<BEGIN_TEXT>", 1)[1]
        snippet = snippet.split("<END_TEXT>", 1)[0]
        return snippet.strip()
    return content.strip()


class Client:
    def __init__(self, *, base_url: str, headers: dict[str, str], timeout: Timeout) -> None:
        self.base_url = base_url
        self.headers = headers
        self.timeout = timeout

    def post(self, path: str, json: dict) -> Response:
        cleaned = _extract_text(json.get("messages") or [])
        restored = cleaned.replace("[[UNK]]", "(restored)")
        data = {"choices": [{"message": {"content": restored}}]}
        return Response(data)

    def close(self) -> None:
        return

