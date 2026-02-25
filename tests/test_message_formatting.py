import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).resolve().parents[1]))

from smtp_server import CustomSMTPHandler, ServerConfig, _HTMLToTextParser


def test_long_html_is_sanitized_truncated_and_sent_with_html_file():
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
    handler.bot.send_document.assert_awaited_once()
    assert handler.bot.send_document.await_args.kwargs["document"].name == "message.html"

    handler.bot.send_message.assert_awaited_once()
    text = handler.bot.send_message.await_args.kwargs["text"]
    assert "<" not in text
    assert ">" not in text
    assert len(text) == 4096
    assert text.endswith("...")
    assert handler.bot.send_message.await_args.kwargs["parse_mode"] is None


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
    expected_text = f"{message_dict['subject']}\n{long_body}"
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
    assert handler.bot.send_document.await_args_list[0].kwargs["document"].name == "message.html"
    assert handler.bot.send_document.await_args.kwargs["caption"] is None


def test_html_parser_skips_style_and_script_content():
    parser = _HTMLToTextParser()
    parser.feed(
        """
        <html>
          <head>
            <style>.hidden{display:none}</style>
            <title>Ignore this title</title>
          </head>
          <body>
            <h1>Your authentication code</h1>
            <p>Please use the code below</p>
            <script>console.log('ignore this script')</script>
          </body>
        </html>
        """
    )

    text = parser.get_text()

    assert "hidden" not in text
    assert "Ignore this title" not in text
    assert "ignore this script" not in text
    assert "Your authentication code" in text
    assert "Please use the code below" in text
