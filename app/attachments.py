"""Public-share attachments, portable binary parts, and document text.

Binary content lives inside the archive as data URLs. A JSON export therefore
remains self-contained even after signed links expire or a backup is copied.
No provider file IDs, browser cookies, or authentication are needed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import mimetypes
import re
import zipfile
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from pypdf import PdfReader

from .models import ConversationArchive

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_TEXT = 100_000
SAFE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Hosts observed in DeepSeek's public client, not an arbitrary URL proxy.
DOWNLOAD_HOSTS = {"files.deepseeksvc.com", "cdn.deepseek.com", "chat.deepseek.com"}


def source_url(file: dict[str, Any]) -> str:
    signed = file.get("signed_path")
    if isinstance(signed, str) and signed.startswith("/file?"):
        return "https://files.deepseeksvc.com/api" + signed + "&ty=r"
    for key in ("download_url", "url", "image_url", "signed_path"):
        value = file.get(key)
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str):
            return value
    return ""


def share_attachments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize FILE fragments used by the live share API and older sidecars.

    Unknown vendor fields are retained in `original`, so a later importer can
    recover newly supported formats from the archive or immutable raw snapshot.
    """
    candidates: list[dict[str, Any]] = []
    containers = [raw, *(x for x in (raw.get("fragments") or []) if isinstance(x, dict))]
    for container in containers:
        for key in ("files", "attachments", "file_infos", "ref_files"):
            for file in container.get(key) or []:
                if isinstance(file, dict):
                    candidates.append(file)
        if str(container.get("type", "")).upper() in {"IMAGE", "FILE", "OCR"}:
            value = container.get("content")
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, str) and value.strip():
                candidates.append({"ocr_text": value} if container["type"].upper() == "OCR" else {"url": value})
    result = []
    seen = set()
    for file in candidates:
        identity = str(file.get("id") or source_url(file) or repr(file))
        if identity in seen:
            continue
        seen.add(identity)
        name = str(file.get("file_name") or file.get("name") or "未命名附件")[:240]
        mime = str(file.get("mime_type") or file.get("content_type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        text = next((file[k] for k in ("ocr_text", "ocr_content", "extracted_text", "text", "content") if isinstance(file.get(k), str)), "")
        result.append({"name": name, "kind": "image" if file.get("is_image") or mime.startswith("image/") else "file",
                       "mime_type": mime, "original_size": file.get("file_size", file.get("size")),
                       "source_url": source_url(file), "status": "pending", "text": text[:MAX_TEXT],
                       "text_source": "deepseek_ocr" if text else None, "original": file})
    return result


def extract_text(data: bytes, name: str, mime: str) -> str:
    """Extract existing text, not image OCR; scanned PDFs need a vision model."""
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        # Many viewable PDFs have an owner password but an empty user password.
        # Try the empty password only; protected documents remain preserved.
        if reader.is_encrypted and not reader.decrypt(""):
            return ""
        result = []
        remaining = MAX_TEXT
        for page in reader.pages[:100]:
            content = page.get_contents()
            if content is not None and len(content.get_data()) > 8 * 1024 * 1024:
                continue
            text = (page.extract_text() or "")[:remaining]
            result.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break
        return "\n\n".join(result)[:MAX_TEXT]
    if name.lower().endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 8 * 1024 * 1024:
                return ""
            root = ElementTree.fromstring(archive.read(info))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))[:MAX_TEXT]
    if mime.startswith("text/") or name.lower().endswith((".txt", ".md", ".csv", ".json", ".log")):
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return data.decode(encoding)[:MAX_TEXT]
            except UnicodeDecodeError:
                pass
    return ""


def binary_part(data: bytes, mime: str, name: str) -> dict[str, Any]:
    uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    if mime in SAFE_IMAGES:
        return {"type": "image_url", "image_url": {"url": uri}}
    return {"type": "file", "file": {"filename": name, "file_data": uri}}


def decode_part(part: dict[str, Any]) -> tuple[str, bytes]:
    uri = (part.get("image_url") or {}).get("url") if part.get("type") == "image_url" else (part.get("file") or {}).get("file_data")
    match = re.fullmatch(r"data:([\w.+/-]+);base64,([A-Za-z0-9+/=\r\n]+)", uri or "")
    if not match or len(match[2]) > MAX_FILE_BYTES * 4 // 3 + 16:
        raise ValueError("附件未保存在本机，或超过大小限制")
    return match[1], base64.b64decode(match[2], validate=True)


async def download_public(url: str, client: httpx.AsyncClient, *, filename: str = "") -> tuple[bytes, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("下载地址不在已验证的 DeepSeek 文件域名内")
    async with client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        mime = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip().lower()
        if mime == "text/html" or (mime == "application/json" and not filename.lower().endswith(".json")):
            raise ValueError("文件服务返回了页面或错误信息")
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > MAX_FILE_BYTES:
                raise ValueError("附件超过 20 MB，未自动下载")
        if not chunks:
            raise ValueError("文件服务返回空文件")
        return bytes(chunks), mime


async def preserve_attachments(archive: ConversationArchive, *, client: httpx.AsyncClient | None = None) -> None:
    """Best effort per file: one inaccessible attachment never loses a chat."""
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
    total = 0
    try:
        for message, metadata in zip(archive.messages, archive.message_metadata):
            files = metadata.details.get("attachments", [])
            if not files:
                continue
            parts = [{"type": "text", "text": message.text_content()}] if message.text_content() else []
            for file in files:
                try:
                    if total >= MAX_ARCHIVE_BYTES:
                        raise ValueError("单次导入附件总量达到 60 MB")
                    url = file["source_url"]
                    if not url:
                        raise ValueError("分享内容未公开原文件下载地址")
                    try:
                        data, mime = await download_public(url, http, filename=file["name"])
                        file["variant"] = "original"
                    except httpx.HTTPStatusError:
                        # The observed public API disallows ty=r for some
                        # images but exposes ty=p. Never label that as original.
                        if file["kind"] != "image" or not url.startswith("https://files.deepseeksvc.com/api/file?"):
                            raise
                        data, mime = await download_public(url.replace("&ty=r", "&ty=p"), http, filename=file["name"])
                        file["variant"] = "preview"
                    if total + len(data) > MAX_ARCHIVE_BYTES:
                        raise ValueError("单次导入附件总量超过 60 MB")
                    total += len(data)
                    file.update(status="saved", mime_type=mime, size=len(data), sha256=hashlib.sha256(data).hexdigest(), content_index=len(parts))
                    parts.append(binary_part(data, mime, file["name"]))
                    if not file.get("text") and file["kind"] != "image":
                        try:
                            file["text"] = await asyncio.to_thread(extract_text, data, file["name"], mime)
                            file["text_source"] = "local_extract" if file["text"] else None
                        except Exception:
                            file["text_error"] = "无法提取文字，原文件已保留"
                except (ValueError, httpx.HTTPError) as exc:
                    file.update(status="unavailable", error=str(exc) if isinstance(exc, ValueError) else "下载失败或分享文件已失效")
                context = f"[附件：{file['name']}]"
                if file.get("text"):
                    context += "\n" + file["text"]
                else:
                    context += "\n[没有可用的识别文字；文本模式无法读取附件内容]"
                file["text_index"] = len(parts)
                parts.append({"type": "text", "text": context})
            message.content = parts
    finally:
        if owns_client:
            await http.aclose()
