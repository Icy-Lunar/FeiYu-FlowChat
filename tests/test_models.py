import pytest
from pydantic import ValidationError

from app.models import BatchConversationRequest, ConversationArchive


def test_archive_repairs_message_metadata_alignment() -> None:
    archive = ConversationArchive.model_validate({
        "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ],
        "message_metadata": [{"details": {"source": "fixture"}}],
    })

    assert len(archive.message_metadata) == len(archive.messages) == 2
    assert archive.message_metadata[0].details["source"] == "fixture"


def test_batch_request_normalizes_ids_and_tags() -> None:
    body = BatchConversationRequest(
        ids=[" first ", "first", "second"],
        action="organize",
        tags=[" Python ", "Python", "  长期   任务  "],
    )

    assert body.ids == ["first", "second"]
    assert body.tags == ["Python", "长期 任务"]


def test_batch_request_rejects_empty_ids_after_cleanup() -> None:
    with pytest.raises(ValidationError):
        BatchConversationRequest(ids=["  "], action="trash")
