import asyncio
import html
import sys
from pathlib import Path
from unittest.mock import AsyncMock, call

sys.path.append(str(Path(__file__).resolve().parents[1]))

from smtp_server import CustomSMTPHandler, ServerConfig


def _chunk_text(text: str, max_length: int = 4096) -> list[str]:
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]


def test_long_html_is_sanitized_and_chunked():
    handler = CustomSMTPHandler(ServerConfig())
    handler.bot = AsyncMock()

    subject = "Тема <tag>"
    html_body = "<b>сообщение</b>" * 800
    message_dict = {
        "subject": subject,
        "text_body": None,
        "html_body": html_body,
        "attachments": []
    }

    result = asyncio.run(handler.send_to_telegram("123", None, message_dict))

    assert result is True
    expected_text = f"{html.escape(subject)}\n{html.escape(html_body)}"
    chunks = _chunk_text(expected_text)
    assert handler.bot.send_message.await_count == len(chunks)
    expected_calls = [
        call(
            chat_id="123",
            text=chunk,
            parse_mode="HTML",
            disable_notification=False,
            message_thread_id=None
        )
        for chunk in chunks
    ]
    handler.bot.send_message.assert_has_awaits(expected_calls)


def test_long_text_with_attachments_sends_caption_and_full_text():
    handler = CustomSMTPHandler(ServerConfig())
    handler.bot = AsyncMock()

    long_body = "A" * 2000
    message_dict = {
        "subject": "Длинная тема",
        "text_body": long_body,
        "html_body": None,
        "attachments": [
            {
                "filename": "test.pdf",
                "content_type": "application/pdf",
                "content": b"%PDF-1.4 test content",
                "content_disposition": "attachment",
                "content_id": "",
                "size": 24,
                "encoding": "utf-8",
                "charset": "utf-8"
            }
        ]
    }

    result = asyncio.run(handler.send_to_telegram("456", None, message_dict))

    assert result is True
    expected_text = f"{html.escape(message_dict['subject'])}\n{html.escape(long_body)}"
    handler.bot.send_message.assert_awaited_once()
    sent_text = handler.bot.send_message.await_args.kwargs["text"]
    assert sent_text == expected_text
    handler.bot.send_document.assert_awaited_once()
    caption = handler.bot.send_document.await_args.kwargs["caption"]
    assert caption is not None
    assert len(caption) <= 1024


def test_html_only_message_sends_plain_text_once_and_html_attachment_without_caption():
    handler = CustomSMTPHandler(ServerConfig())
    handler.bot = AsyncMock()

    message_dict = {
        "subject": "Тема",
        "text_body": None,
        "plain_from_html": "aaaa\naaa\nС уважением",
        "html_body": "<div>aaaa</div><div>aaa</div>",
        "attachments": [
            {
                "filename": "message.html",
                "content_type": "text/html",
                "content": b"<html><body>aaaa</body></html>",
                "content_disposition": "attachment",
                "content_id": "",
                "size": 30,
                "encoding": "utf-8",
                "charset": "utf-8",
                "generated_html": True,
            }
        ]
    }

    result = asyncio.run(handler.send_to_telegram("789", None, message_dict))

    assert result is True
    handler.bot.send_message.assert_awaited_once()
    handler.bot.send_document.assert_awaited_once()
    assert handler.bot.send_document.await_args.kwargs["caption"] is None
