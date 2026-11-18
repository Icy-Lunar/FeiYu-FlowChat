import pytest

from app.deepseek import parse_share_payload, parse_share_url
from app.errors import ImportError


def test_parse_share_url_accepts_canonical_url() -> None:
    share_id, canonical = parse_share_url("https://chat.deepseek.com/share/mugedvey4x3sljprdp?from=copy")
    assert share_id == "mugedvey4x3sljprdp"
    assert canonical == "https://chat.deepseek.com/share/mugedvey4x3sljprdp"


@pytest.mark.parametrize(
    "url",
    [
        "http://chat.deepseek.com/share/abcdefgh",
        "https://example.com/share/abcdefgh",
        "https://chat.deepseek.com/api/v0/share/content?share_id=abcdefgh",
    ],
)
def test_parse_share_url_rejects_non_share_urls(url: str) -> None:
    with pytest.raises(ImportError):
        parse_share_url(url)


def test_parse_share_payload_keeps_openai_messages_and_reasoning() -> None:
    payload = {
        "code": 0,
        "data": {
            "biz_code": 0,
            "biz_data": {
                "title": "Shared Conversation",
                "model_type": "default",
                "messages": [
                    {
                        "message_id": 1,
                        "role": "USER",
                        "inserted_at": 1700000000,
                        "status": "FINISHED",
                        "fragments": [{"id": 1, "type": "REQUEST", "content": "你好"}],
                    },
                    {
                        "message_id": 2,
                        "role": "ASSISTANT",
                        "inserted_at": 1700000001,
                        "status": "FINISHED",
                        "fragments": [
                            {"id": 2, "type": "THINK", "content": "先问候"},
                            {"id": 3, "type": "RESPONSE", "content": "你好！"},
                        ],
                    },
                ],
            },
        },
    }
    archive = parse_share_payload(
        payload,
        share_id="mugedvey4x3sljprdp",
        source_url="https://chat.deepseek.com/share/mugedvey4x3sljprdp",
    )
    assert archive.title == "你好"
    assert [item.request_dict() for item in archive.messages] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]
    assert archive.message_metadata[1].reasoning_content == "先问候"
    assert archive.source["share_id"] == "mugedvey4x3sljprdp"
