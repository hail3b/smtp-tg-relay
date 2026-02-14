import asyncio
from email.message import EmailMessage
from unittest.mock import AsyncMock

import pytest
from aiosmtpd.smtp import Envelope

from smtp_server import CustomSMTPHandler, EmailValidator, ServerConfig, Stats


def _make_handler(local_domains=None) -> CustomSMTPHandler:
    config = ServerConfig(local_domains=local_domains or ["example.com"])
    handler = CustomSMTPHandler(config)
    handler.bot = AsyncMock()
    return handler


def _make_email_bytes(from_addr: str | None = None, to_addr: str | None = None) -> bytes:
    message = EmailMessage()
    if from_addr:
        message["From"] = from_addr
    if to_addr:
        message["To"] = to_addr
    message["Subject"] = "Test"
    message.set_content("Hello from test suite")
    return message.as_bytes()


def test_server_config_validation():
    with pytest.raises(ValueError):
        ServerConfig(max_message_size=0, local_domains=["example.com"])
    with pytest.raises(ValueError):
        ServerConfig(max_stored_messages=0, local_domains=["example.com"])
    with pytest.raises(ValueError):
        ServerConfig(local_domains=[])


@pytest.mark.parametrize(
    "address,expected",
    [
        ("user@example.com", True),
        ("USER@EXAMPLE.COM", True),
        ("invalid", False),
        ("missing@tld", False),
        ("", False),
    ],
)
def test_email_validator(address, expected):
    assert EmailValidator.is_valid_email(address) is expected


def test_stats_reporting_and_reset():
    stats = Stats()
    stats.record_message("10.0.0.1", ["a@example.com", "b@example.com"])
    stats.record_message("10.0.0.1", ["a@example.com"])
    stats.record_delivery(True)
    stats.record_delivery(False)

    report = stats.generate_report()
    assert "Всего сообщений: 2" in report
    assert "Доставлено: 1" in report
    assert "Не доставлено: 1" in report
    assert "10.0.0.1" in report
    assert "a@example.com" in report

    stats.reset()
    assert stats.total_messages == 0
    assert stats.delivered_messages == 0
    assert stats.failed_messages == 0
    assert stats.ip_stats == {}
    assert stats.recipient_stats == {}


def test_text_helpers():
    handler = _make_handler()
    assert handler._sanitize_text("<b>") == "&lt;b&gt;"
    assert handler._truncate_text("A" * 10, max_length=8) == "AAAAA..."
    assert handler._truncate_text(None) == ""
    assert handler._split_text("") == []
    assert handler._split_text("ABC", max_length=2) == ["AB", "C"]
    assert handler._build_message_text("Subject", "Body") == "Subject\nBody"
    assert handler._build_message_text("Only", None) == "Only"
    assert handler._build_message_text(None, "Body") == "Body"


def test_validate_envelope_limits_and_headers():
    handler = _make_handler()
    envelope = Envelope()
    envelope.content = b"A" * (handler.config.max_message_size + 1)
    ok, message = asyncio.run(handler.validate_envelope(envelope))
    assert ok is False
    assert message.startswith("552")

    envelope.content = b"A" * 10
    ok, message = asyncio.run(handler.validate_envelope(envelope))
    assert ok is False
    assert message.startswith("451")

    envelope.content = _make_email_bytes(to_addr="to@example.com")
    ok, message = asyncio.run(handler.validate_envelope(envelope))
    assert ok is False
    assert "Missing required header: From" in message

    envelope.content = _make_email_bytes(from_addr="from@example.com")
    envelope.rcpt_tos = ["rcpt@example.com"]
    ok, message = asyncio.run(handler.validate_envelope(envelope))
    assert ok is True
    assert message == ""


def test_extract_message_content_with_html_and_attachment():
    handler = _make_handler()
    message = EmailMessage()
    message["From"] = "from@example.com"
    message["To"] = "to@example.com"
    message["Subject"] = "HTML"
    message.set_content("Plain text")
    message.add_alternative("<b>HTML</b>", subtype="html")
    message.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="file.bin")

    parsed = handler.extract_message_content(message)
    assert parsed["text_body"] == "Plain text"
    assert parsed["html_body"] is not None
    assert parsed["plain_from_html"] == "HTML"
    assert any(att["filename"] == "file.bin" for att in parsed["attachments"])
    assert any(att["filename"] == "message.html" for att in parsed["attachments"])


def test_process_attachment_assigns_filename():
    handler = _make_handler()
    message = EmailMessage()
    message.add_attachment(b"content", maintype="application", subtype="octet-stream")

    parsed = {"attachments": []}
    for part in message.iter_attachments():
        handler._process_attachment(part, parsed)

    assert len(parsed["attachments"]) == 1
    assert parsed["attachments"][0]["filename"].startswith("attachment_0")


def test_prepare_media_files_grouping():
    handler = _make_handler()
    attachments = [
        {"filename": "img.png", "content_type": "image/png", "content": b"1"},
        {"filename": "video.mp4", "content_type": "video/mp4", "content": b"2"},
        {"filename": "audio.ogg", "content_type": "audio/ogg", "content": b"3"},
        {"filename": "anim.gif", "content_type": "image/gif", "content": b"4"},
        {"filename": "doc.pdf", "content_type": "application/pdf", "content": b"5"},
    ]

    grouped = asyncio.run(handler._prepare_media_files(attachments))
    assert len(grouped["photo"]) == 1
    assert len(grouped["video"]) == 1
    assert len(grouped["audio"]) == 1
    assert len(grouped["animation"]) == 1
    assert len(grouped["document"]) == 1


def test_send_to_telegram_splits_long_text():
    handler = _make_handler()
    handler.bot.send_message = AsyncMock()
    message_dict = {
        "subject": "Subject",
        "text_body": "A" * 5000,
        "html_body": None,
        "attachments": [],
    }

    result = asyncio.run(handler.send_to_telegram("123", None, message_dict))
    assert result is True
    expected_text = handler._build_message_text("Subject", message_dict["text_body"])
    assert handler.bot.send_message.await_count == len(handler._split_text(expected_text))


def test_send_to_telegram_multiple_photos_uses_media_group():
    handler = _make_handler()
    handler.bot.send_media_group = AsyncMock()
    handler.bot.send_message = AsyncMock()
    message_dict = {
        "subject": "Photos",
        "text_body": "Body",
        "html_body": None,
        "attachments": [
            {"filename": "a.png", "content_type": "image/png", "content": b"1"},
            {"filename": "b.png", "content_type": "image/png", "content": b"2"},
        ],
    }

    result = asyncio.run(handler.send_to_telegram("123", None, message_dict))
    assert result is True
    handler.bot.send_media_group.assert_awaited_once()
    handler.bot.send_message.assert_not_awaited()


def test_get_local_recipient_name_without_alias():
    handler = _make_handler(local_domains=["example.com"])

    recipient = handler._get_local_recipient_name("id123@example.com")

    assert recipient is not None
    assert recipient.chat_id == "123"
    assert recipient.message_thread_id is None
    assert recipient.silent is False


def test_get_local_recipient_name_with_alias_and_flags():
    handler = _make_handler(local_domains=["example.com"])
    handler.config.recipient_aliases = {"admin": "-1001234567890!55"}

    recipient = handler._get_local_recipient_name("admin.s@example.com")

    assert recipient is not None
    assert recipient.chat_id == "-1001234567890"
    assert recipient.message_thread_id == "55"
    assert recipient.silent is True


def test_handle_data_processes_local_delivery():
    handler = _make_handler(local_domains=["example.com"])
    handler.send_to_telegram = AsyncMock(return_value=True)
    handler.bot.send_message = AsyncMock()

    envelope = Envelope()
    envelope.mail_from = "sender@example.com"
    envelope.rcpt_tos = ["id123@example.com"]
    envelope.content = _make_email_bytes(from_addr="sender@example.com", to_addr="id123@example.com")

    class DummySession:
        peer = ("127.0.0.1", 25000)
        host_name = "localhost"

    response = asyncio.run(handler.handle_DATA(None, DummySession(), envelope))
    assert response.startswith("250")
    assert handler.stats.delivered_messages == 1
    assert handler.messages[-1]["is_local_delivery"] is True
    handler.send_to_telegram.assert_awaited_once()
