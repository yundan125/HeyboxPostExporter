from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .mhtml import MhtmlDocument
from .models import ExportData, Media
from .utils import safe_filename


class AssetArchiver:
    def __init__(self, output_dir: Path, logger: logging.Logger, mhtml: MhtmlDocument | None = None):
        self.output_dir = output_dir
        self.logger = logger
        self.mhtml = mhtml

    def archive(self, data: ExportData, *, post_images: bool, comment_images: bool) -> None:
        jobs: list[tuple[Media, str]] = []
        if post_images:
            jobs.extend((item, "post") for item in data.post.images + data.post.videos)
        if comment_images:
            for comment in data.comments:
                jobs.extend((item, "comments") for item in comment.images)
                for reply in comment.replies:
                    jobs.extend((item, "comments") for item in reply.images)
        unique: dict[str, tuple[Media, str]] = {}
        for media, group in jobs:
            unique.setdefault(media.url, (media, group))
        total = len(unique)
        if not total:
            return
        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for index, (url, (media, group)) in enumerate(unique.items(), start=1):
                self.logger.info("正在下载图片：%s / %s", index, total)
                try:
                    content, content_type = self._read(url, client)
                    target = self._target(url, group, content_type)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    relative = target.relative_to(self.output_dir).as_posix()
                    for candidate, _ in jobs:
                        if candidate.url == url:
                            candidate.local_path = relative
                except Exception as error:
                    self.logger.warning("图片保存失败：%s（%s）", url, error)

    def _read(self, url: str, client: httpx.Client) -> tuple[bytes, str]:
        if self.mhtml:
            resource = self.mhtml.resources.get(url)
            if resource:
                return resource.data, resource.content_type
        response = client.get(url, headers={"Referer": "https://www.xiaoheihe.cn/"})
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "").split(";", 1)[0]

    def _target(self, url: str, group: str, content_type: str) -> Path:
        basename = Path(unquote(urlsplit(url).path)).name or "image"
        stem = safe_filename(Path(basename).stem, "image", 70)
        extension = Path(basename).suffix.lower()
        mime_extension = mimetypes.guess_extension(content_type) if content_type else None
        if mime_extension and content_type.startswith(("image/", "video/")):
            extension = mime_extension
        elif not extension or len(extension) > 8:
            extension = mime_extension or ".bin"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        return self.output_dir / "assets" / group / f"{stem}_{digest}{extension}"
