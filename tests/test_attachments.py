"""Regression coverage for public FILE fragments and portable continuations."""

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import app.main as main
from app.attachments import binary_part, decode_part, download_public, extract_text, preserve_attachments
from app.backup import BackupManager
from app.deepseek import parse_share_payload
from app.llm import _body
from app.models import ConversationArchive, ProviderConfig
from app.storage import ArchiveStore


def share(files):
    return parse_share_payload({"code": 0, "data": {"biz_code": 0, "biz_data": {"messages": [
        {"role": "USER", "fragments": [{"type": "FILE", "files": files}]},
        {"role": "ASSISTANT", "fragments": [{"type": "RESPONSE", "content": "已阅读"}]},
    ]}}}, share_id="abcdefgh", source_url="https://chat.deepseek.com/share/abcdefgh")


@pytest.mark.asyncio
async def test_file_only_message_and_preview_fallback_are_preserved():
    archive = share([
        {"id": "file-img", "file_name": "photo.JPG", "is_image": True, "signed_path": "/file?file_id=img&state=x"},
        {"id": "file-doc", "file_name": "notes.txt", "signed_path": "/file?file_id=doc&state=x"},
    ])
    def handler(request):
        if request.url.params["file_id"] == "doc":
            return httpx.Response(200, content="文档正文".encode(), headers={"content-type": "text/plain"})
        if request.url.params["ty"] == "r":
            return httpx.Response(400)
        return httpx.Response(200, content=b"RIFFmock", headers={"content-type": "image/webp"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await preserve_attachments(archive, client=client)
    assert len(archive.messages) == 2  # Do not discard the attachment-only user turn.
    files = archive.message_metadata[0].details["attachments"]
    assert files[0]["variant"] == "preview"
    assert files[1]["variant"] == "original"
    assert files[1]["text"] == "文档正文"
    assert "文档正文" in archive.messages[0].text_content()
    assert decode_part(archive.messages[0].content[files[0]["content_index"]]) == ("image/webp", b"RIFFmock")


@pytest.mark.asyncio
async def test_missing_files_keep_ocr_and_do_not_abort_conversation():
    archive = share([{"file_name": "scan.png", "is_image": True, "ocr_text": "识别出的历史需求"},
                     {"file_name": "expired.pdf", "url": "https://files.deepseeksvc.com/api/file?expired"}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        await preserve_attachments(archive, client=client)
    assert len(archive.messages) == 2
    assert "识别出的历史需求" in archive.messages[0].text_content()
    assert all(f["status"] == "unavailable" for f in archive.message_metadata[0].details["attachments"])


@pytest.mark.asyncio
async def test_attachment_downloader_rejects_untrusted_hosts_and_redirects():
    hits = []
    def handler(request):
        hits.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await download_public("http://127.0.0.1/private", client)
        with pytest.raises(ValueError):
            await download_public("https://files.deepseeksvc.com.evil.test/file", client)
        assert not hits
        with pytest.raises(httpx.HTTPStatusError):
            await download_public("https://files.deepseeksvc.com/api/file", client)
        assert len(hits) == 1


def test_pdf_text_is_extracted_without_ocr_service():
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Archive document text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.encrypt("", owner_password="owner", algorithm="AES-128")
    output = io.BytesIO(); writer.write(output)
    assert "Archive document text" in extract_text(output.getvalue(), "notes.pdf", "application/pdf")


def test_provider_modes_preserve_archive_and_adapt_deepseek_files():
    archive = ConversationArchive()
    archive.add_message("user", [{"type": "text", "text": "OCR正文"}, binary_part(b"img", "image/png", "image.png"), binary_part(b"pdf", "application/pdf", "notes.pdf")])
    before = archive.model_dump_json()
    config = ProviderConfig(base_url="https://example.test/v1", model="test")
    assert _body(config, archive.messages, stream=False)["messages"][0]["content"] == "OCR正文"
    config.attachment_mode = "images"
    assert [p["type"] for p in _body(config, archive.messages, stream=False)["messages"][0]["content"]] == ["text", "image_url"]
    config.attachment_mode = "files"
    assert "file" in _body(config, archive.messages, stream=False)["messages"][0]["content"][2]
    config.provider_id = "deepseek"
    assert "file_data" in _body(config, archive.messages, stream=False)["messages"][0]["content"][2]
    assert archive.model_dump_json() == before


def test_export_import_and_backup_keep_binary_parts(monkeypatch, tmp_path):
    store = ArchiveStore(tmp_path / "data" / "archives")
    archive = ConversationArchive(title="附件归档")
    archive.add_message("user", [binary_part(b"\x89PNG\r\n\x1a\n", "image/png", "photo.png"), binary_part(b"%PDF", "application/pdf", "笔记.pdf")])
    store.save(archive)
    copy = store.import_openai(archive.model_dump(), "导入")
    assert copy.messages == archive.messages
    manager = BackupManager(store.data_dir, store.data_dir / "settings.json", store)
    backup = manager.create(tmp_path / "backups")
    # Existing backup tests cover conflict policies; this verifies the binary
    # content survives the actual ZIP/manifest round-trip unchanged.
    restored_store = ArchiveStore(tmp_path / "restored" / "archives")
    restored_manager = BackupManager(restored_store.data_dir, restored_store.data_dir / "settings.json", restored_store)
    restored_manager.restore(backup, conflict="skip")
    assert restored_store.get(archive.id).messages == archive.messages
    monkeypatch.setattr(main, "store", restored_store)
    with TestClient(main.app) as client:
        base = f"/api/conversations/{archive.id}/attachments/0"
        image = client.get(base + "/0")
        assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
        assert image.headers["content-disposition"].startswith("inline")
        assert client.get(base + "/1").headers["content-disposition"].startswith("attachment")
        assert client.get(base + "/-1").status_code == 404
        assert client.get(base + "/99").status_code == 404
