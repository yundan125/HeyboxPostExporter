from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path


@dataclass
class MhtmlResource:
    location: str
    content_type: str
    data: bytes


@dataclass
class MhtmlDocument:
    html: str
    source_url: str
    subject: str
    resources: dict[str, MhtmlResource]


def _decoded_payload(part: Message) -> bytes:
    return part.get_payload(decode=True) or b""


def load_mhtml(path: str | Path) -> MhtmlDocument:
    source = Path(path)
    with source.open("rb") as stream:
        message = BytesParser(policy=policy.default).parse(stream)
    html_parts = [part for part in message.walk() if part.get_content_type() == "text/html"]
    if not html_parts:
        raise ValueError(f"MHTML 中没有 HTML 主文档：{source}")
    main = max(html_parts, key=lambda part: len(_decoded_payload(part)))
    payload = _decoded_payload(main)
    # Blink MHTML declares UTF-8 in the document even when the MIME parser guesses
    # a legacy charset from the surrounding Windows environment.
    html = payload.decode("utf-8", errors="replace")
    resources: dict[str, MhtmlResource] = {}
    for part in message.walk():
        if part.is_multipart() or part is main:
            continue
        location = str(part.get("Content-Location") or "")
        content_id = str(part.get("Content-ID") or "").strip("<>")
        resource = MhtmlResource(location, part.get_content_type(), _decoded_payload(part))
        if location:
            resources[location] = resource
        if content_id:
            resources[f"cid:{content_id}"] = resource
    return MhtmlDocument(
        html=html,
        source_url=str(main.get("Content-Location") or message.get("Snapshot-Content-Location") or ""),
        subject=str(message.get("Subject") or ""),
        resources=resources,
    )
