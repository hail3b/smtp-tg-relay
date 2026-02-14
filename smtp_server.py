import logging
import aiosmtpd.controller
from aiosmtpd.smtp import Envelope, Session, SMTP
from email.parser import BytesParser
from email.policy import default
from email.utils import parseaddr
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime
import os
import asyncio
from collections import deque
import re
from config import (
    SMTP_HOST,
    SMTP_PORT,
    MAX_MESSAGE_SIZE,
    MAX_STORED_MESSAGES,
    get_local_domains,
    get_recipient_aliases,
    TELEGRAM_BOT_TOKEN,
    STATS_ADMIN_CHAT_ID,
    STATS_INTERVAL
)

from telegram import Bot, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.error import TelegramError, RetryAfter
import html
import mimetypes
from io import BytesIO
from pathlib import Path
import chardet
from html.parser import HTMLParser

@dataclass
class ServerConfig:
    """SMTP Server configuration"""
    hostname: str = SMTP_HOST
    port: int = SMTP_PORT
    max_message_size: int = MAX_MESSAGE_SIZE
    max_stored_messages: int = MAX_STORED_MESSAGES
    local_domains: List[str] = None
    recipient_aliases: Dict[str, str] = None
    
    def __post_init__(self):
        if self.max_message_size <= 0:
            raise ValueError("max_message_size must be positive")
        if self.max_stored_messages <= 0:
            raise ValueError("max_stored_messages must be positive")
        # Инициализируем список доменов из конфигурации
        if self.local_domains is None:
            self.local_domains = get_local_domains()
        # Проверяем, что список доменов не пустой
        if not self.local_domains:
            raise ValueError("local_domains cannot be empty")
        if self.recipient_aliases is None:
            self.recipient_aliases = get_recipient_aliases()
        print(f'hostname: {self.hostname}')

class EmailValidator:
    """Email validation utility"""
    EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    
    @classmethod
    def is_valid_email(cls, email: str) -> bool:
        """Validate email address format"""
        if not email:
            return False
        _, addr = parseaddr(email)
        return bool(cls.EMAIL_REGEX.match(addr))

@dataclass
class LocalRecipient:
    """Структура данных для локального получателя"""
    chat_id: str
    message_thread_id: Optional[str] = None
    silent: bool = False
    
    @classmethod
    def parse(cls, local_name: str) -> Optional['LocalRecipient']:
        """Парсит локальное имя и возвращает структуру LocalRecipient"""
        if not local_name:
            return None
            
        # Выделяем флаги, отделённые точкой
        flags = set()
        if '.' in local_name:
            local_name, flag_part = local_name.split('.', 1)
            flags = set(flag_part.split('.'))

        parts = local_name.split('!')
            
        if len(parts) == 1:
            parts = local_name.split('_')
                
        # Если только одна часть, предполагается, что это chat_id
        if len(parts) == 1:
            chat_id = parts[0].lstrip("id")  # Убираем префикс "id" если он есть
            return cls(chat_id=chat_id, silent=('s' in flags or 'silent' in flags))
        
        # Если две части, предполагается, что это chat_id и message_thread_id
        elif len(parts) == 2:
            chat_id = parts[0].lstrip("id")  # Убираем префикс "id" если он есть
            message_thread_id = parts[1]
            return cls(chat_id=chat_id, message_thread_id=message_thread_id, silent=('s' in flags or 'silent' in flags))

        return None


class _HTMLToTextParser(HTMLParser):
    """Конвертирует HTML в человекочитаемый plain text"""

    BLOCK_TAGS = {"p", "div", "br", "li", "tr", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self.current_link: Optional[Dict[str, str]] = None

    def _append_break(self):
        if not self.parts or self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self._append_break()
        if tag == "a":
            href = dict(attrs).get("href", "")
            self.current_link = {"href": href, "text": ""}

    def handle_endtag(self, tag):
        if tag in self.BLOCK_TAGS:
            self._append_break()
        if tag == "a" and self.current_link:
            href = self.current_link.get("href", "").strip()
            text = self.current_link.get("text", "").strip()
            if href and href not in text:
                self.parts.append(f" ({href})")
            self.current_link = None

    def handle_data(self, data):
        if not data:
            return
        self.parts.append(data)
        if self.current_link:
            self.current_link["text"] += data

    def get_text(self) -> str:
        raw = "".join(self.parts)
        lines = [line.strip() for line in raw.splitlines()]
        compact = "\n".join(line for line in lines if line)
        return html.unescape(compact)


@dataclass
class Stats:
    """Statistics for processed messages"""
    total_messages: int = 0
    delivered_messages: int = 0
    failed_messages: int = 0
    ip_stats: Dict[str, int] = None
    recipient_stats: Dict[str, int] = None

    def __post_init__(self):
        self.ip_stats = {}
        self.recipient_stats = {}

    def reset(self) -> None:
        self.total_messages = 0
        self.delivered_messages = 0
        self.failed_messages = 0
        self.ip_stats.clear()
        self.recipient_stats.clear()

    def record_message(self, ip: str, recipients: List[str]) -> None:
        self.total_messages += 1
        if ip:
            self.ip_stats[ip] = self.ip_stats.get(ip, 0) + 1
        for rcpt in recipients:
            self.recipient_stats[rcpt] = self.recipient_stats.get(rcpt, 0) + 1

    def record_delivery(self, success: bool) -> None:
        if success:
            self.delivered_messages += 1
        else:
            self.failed_messages += 1

    def generate_report(self) -> str:
        lines = [
            f"Всего сообщений: {self.total_messages}",
            f"Доставлено: {self.delivered_messages}",
            f"Не доставлено: {self.failed_messages}",
            "",
            "Статистика по IP:",
        ]
        for ip, count in self.ip_stats.items():
            lines.append(f" - {ip}: {count}")
        lines.append("\nСтатистика по получателям:")
        for rcpt, count in self.recipient_stats.items():
            lines.append(f" - {rcpt}: {count}")
        return "\n".join(lines)

class CustomSMTPHandler:
    """SMTP request handler"""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.messages: deque = deque(maxlen=config.max_stored_messages)
        self.logger = logging.getLogger(__name__)
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.stats = Stats()
        self.stats_admin_chat_id = STATS_ADMIN_CHAT_ID
        self.stats_interval = STATS_INTERVAL
        self._stats_task: Optional[asyncio.Task] = None
        self._telegram_retry_attempts = 3
        self._telegram_retry_base_delay = 1.0
        self._telegram_retry_max_delay = 10.0

    def start_stats(self) -> None:
        """Start periodic statistics reporting"""
        if self.stats_admin_chat_id and self.stats_interval > 0:
            self._stats_task = asyncio.create_task(self._stats_loop())

    def stop_stats(self) -> None:
        """Stop periodic statistics reporting"""
        if self._stats_task:
            self._stats_task.cancel()

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self.stats_interval)
            await self.send_stats()

    async def send_stats(self) -> None:
        if not self.stats_admin_chat_id:
            return
        report = self.stats.generate_report()
        if not report:
            return
        try:
            await self.bot.send_message(chat_id=self.stats_admin_chat_id, text=report)
        except Exception as e:
            self.logger.error(f"Failed to send stats: {e}")
        finally:
            self.stats.reset()

    async def _execute_with_retry(self, action, description: str) -> None:
        """Выполняет действие с повторами для Telegram API."""
        delay = self._telegram_retry_base_delay
        for attempt in range(1, self._telegram_retry_attempts + 1):
            try:
                await action()
                return
            except RetryAfter as e:
                retry_after = float(getattr(e, "retry_after", delay))
                self.logger.warning(
                    f"Telegram rate limit during {description}; retrying in {retry_after:.1f}s "
                    f"(attempt {attempt}/{self._telegram_retry_attempts})"
                )
                await asyncio.sleep(retry_after)
            except TelegramError as e:
                if attempt >= self._telegram_retry_attempts:
                    raise
                self.logger.warning(
                    f"Telegram error during {description}: {e}. Retrying in {delay:.1f}s "
                    f"(attempt {attempt}/{self._telegram_retry_attempts})"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._telegram_retry_max_delay)

    async def validate_envelope(self, envelope: Envelope) -> Tuple[bool, str]:
        if len(envelope.content) > self.config.max_message_size:
            return False, '552 Message size exceeds fixed maximum message size'
    
        if len(envelope.content) < 50:
            return False, '451 Invalid message content'
    
        try:
            email_message = BytesParser(policy=default).parsebytes(envelope.content)
            if not email_message.get('From'):
                return False, '451 Missing required header: From'
            # Если заголовок 'To' отсутствует, но в envelope есть получатели, считаем, что он задан
            if not email_message.get('To') and not envelope.rcpt_tos:
                return False, '451 Missing required header: To'
        except Exception as e:
            self.logger.error(f'Error parsing message: {e}')
            return False, '451 Invalid message format'
    
        return True, ''

    def _process_html_content(self, raw_payload, part, parsed_email: Dict) -> None:
        """Обрабатывает HTML-содержимое сообщения"""
        # Пытаемся узнать кодировку из заголовка
        declared_charset = part.get_content_charset()
        
        # Если в заголовке нет charset, пробуем автоматически определить (через chardet)
        if not declared_charset:
            detected = chardet.detect(raw_payload)
            declared_charset = detected['encoding'] or 'utf-8'
        
        # Декодируем в ту кодировку, которая нашлась
        html_content = raw_payload.decode(declared_charset, errors='replace')
        
        # Сохраняем «чистый» HTML
        parsed_email["html_body"] = html_content
        parser = _HTMLToTextParser()
        parser.feed(html_content)
        parsed_email["plain_from_html"] = parser.get_text()

        # При необходимости перекодируем в UTF-8 (если хотим сохранить/передать именно в UTF-8)
        html_as_utf8 = html_content.encode('utf-8')
        
        # Предполагаем, что html_as_utf8 — это байты в UTF-8
        html_content = html_as_utf8.decode('utf-8', errors='replace')
        wrapped_html = f"""<!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Сообщение</title>
        </head>
        <body>
        {html_content}
        </body>
        </html>"""
        wrapped_html_as_utf8 = wrapped_html.encode('utf-8')
        html_as_utf8 = wrapped_html_as_utf8

        # Добавляем HTML как вложение
        attachment_info = {
            "filename": "message.html",
            "content_type": "text/html",
            "content": html_as_utf8,
            "content_disposition": "attachment",
            "content_id": "",
            "size": len(html_as_utf8),
            "encoding": "utf-8",
            "charset": "utf-8",
            "generated_html": True,
        }
        parsed_email["attachments"].append(attachment_info)
        

    def extract_message_content(self, email_message) -> Dict:
        """Extract content from email message"""
        parsed_email = {
            "mail_from": "",
            "rcpt_tos": [],
            "subject": email_message.get("subject", ""),
            "from": email_message.get("from", ""),
            "to": email_message.get("to", ""),
            "cc": email_message.get("cc", ""),
            "bcc": email_message.get("bcc", ""),
            "date": email_message.get("date", datetime.now().isoformat()),
            "text_body": None,
            "html_body": None,
            "plain_from_html": None,
            "attachments": [],
            "X-Client-IP": None,
            "X-Host-Name": None
        }

        if email_message.is_multipart():
            for part in email_message.walk():
                self._process_message_part(part, parsed_email)
        else:
            content_type = email_message.get_content_type()
            if content_type == "text/plain":
                parsed_email["text_body"] = email_message.get_payload(decode=True).decode().rstrip()
            elif content_type == "text/html":
                if parsed_email["html_body"] is None:
                    raw_payload = email_message.get_payload(decode=True)
                    self._process_html_content(raw_payload, email_message, parsed_email)
            else:
                self._process_attachment(email_message, parsed_email)

        return parsed_email

    def _process_message_part(self, part, parsed_email: Dict) -> None:
        """Process individual message part"""
        if part.get_content_maintype() == 'multipart':
            return

        content_type = part.get_content_type()
        content_disposition = str(part.get('Content-Disposition', ''))

        if content_type == "text/plain" and 'attachment' not in content_disposition:
            if parsed_email["text_body"] is None:
                parsed_email["text_body"] = part.get_payload(decode=True).decode().rstrip()
        elif content_type == "text/html" and 'attachment' not in content_disposition:
            if parsed_email["html_body"] is None:
                raw_payload = part.get_payload(decode=True)
                self._process_html_content(raw_payload, part, parsed_email)
        elif 'attachment' in content_disposition or 'inline' in content_disposition:
            self._process_attachment(part, parsed_email)

    def _process_attachment(self, part, parsed_email: Dict) -> None:
        """Process email attachment"""
        try:
            filename = part.get_filename()
            if not filename:
                ext = mimetypes.guess_extension(part.get_content_type()) or ''
                filename = f'attachment_{len(parsed_email["attachments"])}{ext}'

            # Получаем содержимое в бинарном виде
            payload = part.get_payload(decode=True)
            
            if payload is None:
                self.logger.warning(f"Empty payload for attachment: {filename}")
                # Пробуем получить payload другим способом
                payload = part.get_payload()
                if isinstance(payload, str):
                    payload = payload.encode('utf-8')
                elif isinstance(payload, list):
                    # Если payload это список, берем первый элемент
                    if payload and hasattr(payload[0], 'get_payload'):
                        payload = payload[0].get_payload(decode=True)
                if payload is None:
                    self.logger.error("No valid payload found")
                    return

            # Проверяем, что payload действительно в бинарном формате
            if isinstance(payload, str):
                payload = payload.encode('utf-8')

            # Проверяем корректность бинарных данных
            if len(payload) == 0:
                self.logger.error(f"Zero-length payload for {filename}")
                return

            attachment_info = {
                "filename": filename,
                "content_type": part.get_content_type(),
                "content": payload,
                "content_disposition": str(part.get('Content-Disposition', 'attachment')),
                "content_id": str(part.get('Content-ID', '')),
                "size": len(payload),
                "encoding": str(part.get('Content-Transfer-Encoding', '')),
                "charset": str(part.get_content_charset() or 'utf-8')
            }
            
            self.logger.info(
                f"Attachment processed:\n"
                f"- Filename: {filename}\n"
                f"- Type: {attachment_info['content_type']}\n"
                f"- Size: {attachment_info['size']} bytes\n"
                f"- Encoding: {attachment_info['encoding']}\n"
                f"- Charset: {attachment_info['charset']}"
            )
            
            parsed_email["attachments"].append(attachment_info)
            
        except Exception as e:
            self.logger.error(f"Error processing attachment: {str(e)}", exc_info=True)

    def _is_local_recipient(self, email: str) -> bool:
        """Проверяет, является ли получатель локальным"""
        _, addr = parseaddr(email)
        return any(addr.endswith(f"@{domain}") for domain in self.config.local_domains)

    def _get_local_recipient_name(self, email: str) -> Optional[LocalRecipient]:
        """Извлекает и парсит имя локального получателя без домена"""
        _, addr = parseaddr(email)
        for domain in self.config.local_domains:
            if addr.endswith(f"@{domain}"):
                local_name = addr.split(f"@{domain}")[0]
                return self._resolve_local_recipient(local_name)
        return None

    def _resolve_local_recipient(self, local_name: str) -> Optional[LocalRecipient]:
        """Разрешает алиас локальной части адреса и парсит получателя."""
        if not local_name:
            return None

        raw_local_name = local_name
        flag_suffix = ''
        if '.' in raw_local_name:
            raw_local_name, flag_part = raw_local_name.split('.', 1)
            flag_suffix = f'.{flag_part}'

        canonical_local_name = self.config.recipient_aliases.get(raw_local_name, raw_local_name)
        return LocalRecipient.parse(f'{canonical_local_name}{flag_suffix}')

    def _handle_local_delivery(self, message_dict: Dict) -> None:
        """Обработка сообщений для локальных получателей"""
        recipient_domains = set()
        local_recipients = []
        
        for rcpt in message_dict['rcpt_tos']:
            _, addr = parseaddr(rcpt)
            domain = addr.split('@')[-1]
            if domain in self.config.local_domains:
                recipient_domains.add(domain)
                recipient = self._get_local_recipient_name(rcpt)
                if recipient:
                    local_recipients.append({
                        'chat_id': recipient.chat_id,
                        'message_thread_id': recipient.message_thread_id,
                        'silent': recipient.silent
                    })
        
        self.logger.info(f"Processing local delivery for domains: {', '.join(recipient_domains)}")
        message_dict['is_local_delivery'] = True
        message_dict['local_recipient_domains'] = list(recipient_domains)
        message_dict['local_recipients'] = local_recipients

    async def _prepare_media_files(self, attachments: List[Dict]) -> Dict[str, List[Dict]]:
        """Подготавливает медиафайлы для отправки, группируя их по типу"""
        # Группируем вложения по типу
        MEDIA_TYPES = {
            'animation': {
                'image/gif'
            },
            'photo': {
                'image/',
                'application/png',
                'application/jpg',
                'application/jpeg'
            },
            'video': {
                'video/',
                'application/mp4',
                'application/mpeg'
            },
            'audio': {
                'audio/',
                'application/ogg',
                'application/mp3',
                'application/wav'
            }
        }

        # Группируем вложения по типу
        media_files = {
            'photo': [],
            'video': [],
            'audio': [],
            'animation': [],
            'document': []
        }
        
        for attachment in attachments:
            if not attachment['content']:
                continue
                
            content = attachment['content']
            filename = attachment['filename']
            content_type = attachment['content_type'].lower()
            
            # Создаем новый BytesIO для каждого файла
            file_data = BytesIO(content)
            file_data.seek(0)  # Убеждаемся, что указатель в начале
            file_data.name = filename
            
            # Проверяем размер файла
            file_size = len(content)
            if file_size == 0:
                self.logger.error(f"Zero-size file detected: {filename}")
                continue
            
            self.logger.info(f"Processing file {filename} of type {content_type}, size: {file_size} bytes")
            
            # Определяем тип медиа
            media_type = 'document'
            for type_name, mime_types in MEDIA_TYPES.items():
                if any(content_type.startswith(mime_type) for mime_type in mime_types):
                    media_type = type_name
                    break
            
            media_files[media_type].append({
                'file': file_data,
                'filename': filename,
                'size': file_size
            })
            
        return media_files

    def _sanitize_text(self, text: Optional[str]) -> str:
        """Санитайзит текст для безопасной отправки в HTML"""
        if not text:
            return ""
        return html.escape(text)

    def _truncate_text(self, text: Optional[str], max_length: int = 1024) -> str:
        """Обрезает текст до максимальной длины с добавлением многоточия"""
        if not text:
            return ""
        if len(text) > max_length:
            return text[:max_length - 3] + "..."
        return text

    def _split_text(self, text: str, max_length: int = 4096) -> List[str]:
        """Разбивает текст на части по максимальной длине"""
        if not text:
            return []
        return [text[i:i + max_length] for i in range(0, len(text), max_length)]

    def _build_message_text(self, subject: Optional[str], body: Optional[str]) -> str:
        """Формирует и санитайзит итоговый текст сообщения"""
        subject_text = self._sanitize_text(subject)
        body_text = self._sanitize_text(body)
        if subject_text and body_text:
            return f"{subject_text}\n{body_text}"
        return subject_text or body_text

    async def _send_text_messages(self, chat_id: str, message_thread_id: Optional[str],
                                  text: str, silent: bool = False) -> None:
        """Отправляет текст, разбивая его на части по лимиту Telegram"""
        for chunk in self._split_text(text):
            await self._execute_with_retry(
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode='HTML',
                    disable_notification=silent,
                    message_thread_id=message_thread_id if message_thread_id else None
                ),
                "send_message"
            )

    async def send_to_telegram(self, chat_id: str, message_thread_id: Optional[str], message_dict: Dict, silent: bool = False) -> bool:
        """Отправляет сообщение в Telegram"""
        try:
            # Формируем текст сообщения
            body = (
                message_dict.get('text_body')
                or message_dict.get('plain_from_html')
                or message_dict.get('html_body')
            )
            text = self._build_message_text(message_dict.get('subject'), body)
            needs_full_text_message = len(text) > 1024
            caption_text = self._truncate_text(text) if needs_full_text_message else text
            caption_text = caption_text or None

            attachments = message_dict.get('attachments', [])
            generated_html_attachments = [att for att in attachments if att.get('generated_html')]
            regular_attachments = [att for att in attachments if not att.get('generated_html')]
            
            # Если нет вложений, отправляем только текст
            if not attachments:
                await self._send_text_messages(chat_id, message_thread_id, text, silent)
                return True

            if not regular_attachments:
                if text:
                    await self._send_text_messages(chat_id, message_thread_id, text, silent)
                for attachment in generated_html_attachments:
                    document = BytesIO(attachment['content'])
                    document.name = attachment['filename']
                    try:
                        await self.bot.send_document(
                            chat_id=chat_id,
                            document=document,
                            caption=None,
                            parse_mode=None,
                            disable_notification=silent,
                            message_thread_id=message_thread_id if message_thread_id else None
                        )
                    finally:
                        document.close()
                return True

            if needs_full_text_message and text:
                await self._send_text_messages(chat_id, message_thread_id, text, silent)

            # Группируем вложения по типу
            media_files = await self._prepare_media_files(regular_attachments)
            
            # Проверяем, все ли файлы одного типа
            non_empty_types = [(type_name, files) for type_name, files in media_files.items() if files]
            if len(non_empty_types) == 1:
                media_type, files = non_empty_types[0]
                
                # Если файл один, отправляем его с текстом
                if len(files) == 1:
                    return await self._send_media(
                        chat_id,
                        message_thread_id,
                        media_type,
                        files[0],
                        caption_text,
                        silent
                    )
                
                # Если несколько файлов одного типа, отправляем их группой (если поддерживается) с текстом в первом файле
                if media_type in {"audio", "animation"}:
                    first_file, *remaining_files = files
                    if caption_text:
                        first_sent = await self._send_media(
                            chat_id,
                            message_thread_id,
                            media_type,
                            first_file,
                            caption_text,
                            silent
                        )
                    else:
                        first_sent = await self._send_media(
                            chat_id,
                            message_thread_id,
                            media_type,
                            first_file,
                            None,
                            silent
                        )
                    rest_sent = True
                    if remaining_files:
                        rest_sent = await self._send_files_individually(
                            chat_id,
                            message_thread_id,
                            media_type,
                            remaining_files,
                            silent
                        )
                    return first_sent and rest_sent

                return await self._send_media_group_with_text(
                    chat_id,
                    message_thread_id,
                    media_type,
                    files,
                    caption_text or "",
                    silent
                )

            # Если файлы разных типов, отправляем текст и группы файлов отдельно
            if not needs_full_text_message and text:
                await self._execute_with_retry(
                    lambda: self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML',
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_message"
                )
            
            # Отправляем каждую группу файлов
            for media_type, files in media_files.items():
                if files:
                    await self._send_media_group(chat_id, message_thread_id, media_type, files, silent)

            for attachment in generated_html_attachments:
                document = BytesIO(attachment['content'])
                document.name = attachment['filename']
                try:
                    await self.bot.send_document(
                        chat_id=chat_id,
                        document=document,
                        caption=None,
                        parse_mode=None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    )
                finally:
                    document.close()

            return True

        except Exception as e:
            self.logger.error(f"Error in send_to_telegram: {str(e)}", exc_info=True)
            return False

    async def _send_media(self, chat_id: str, message_thread_id: Optional[str],
                         media_type: str, file: Dict, text: Optional[str] = None, silent: bool = False) -> bool:
        """Общий метод для отправки медиафайлов"""
        try:
            file['file'].seek(0)
            
            if media_type == 'photo':
                await self._execute_with_retry(
                    lambda: self.bot.send_photo(
                        chat_id=chat_id,
                        photo=file['file'],
                        caption=text,
                        parse_mode='HTML' if text else None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_photo"
                )
            elif media_type == 'video':
                await self._execute_with_retry(
                    lambda: self.bot.send_video(
                        chat_id=chat_id,
                        video=file['file'],
                        caption=text,
                        parse_mode='HTML' if text else None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_video"
                )
            elif media_type == 'audio':
                await self._execute_with_retry(
                    lambda: self.bot.send_audio(
                        chat_id=chat_id,
                        audio=file['file'],
                        caption=text,
                        parse_mode='HTML' if text else None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_audio"
                )
            elif media_type == 'animation':
                await self._execute_with_retry(
                    lambda: self.bot.send_animation(
                        chat_id=chat_id,
                        animation=file['file'],
                        caption=text,
                        parse_mode='HTML' if text else None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_animation"
                )
            else:  # document
                await self._execute_with_retry(
                    lambda: self.bot.send_document(
                        chat_id=chat_id,
                        document=file['file'],
                        caption=text,
                        parse_mode='HTML' if text else None,
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_document"
                )
            return True
        except Exception as e:
            self.logger.error(f"Failed to send media: {str(e)}")
            return False
        finally:
            file['file'].close()

    async def _send_media_group_with_text(self, chat_id: str, message_thread_id: Optional[str],
                                         media_type: str, files: List[Dict], text: str, silent: bool = False) -> bool:
        """Отправляет группу медиафайлов с текстом в первом файле"""
        try:
            
            if media_type == 'photo':
                media_group = [
                    InputMediaPhoto(
                        media=files[0]['file'],
                        caption=text,
                        parse_mode='HTML'
                    )
                ]
                media_group.extend([
                    InputMediaPhoto(
                        media=img['file']
                    ) for img in files[1:]
                ])
            elif media_type == 'video':
                media_group = [
                    InputMediaVideo(
                        media=files[0]['file'],
                        caption=text,
                        parse_mode='HTML'
                    )
                ]
                media_group.extend([
                    InputMediaVideo(
                        media=vid['file']
                    ) for vid in files[1:]
                ])
            elif media_type == 'document':
                media_group = [
                    InputMediaDocument(
                        media=files[0]['file'],
                        caption=text,
                        parse_mode='HTML'
                    )
                ]
                media_group.extend([
                    InputMediaDocument(
                        media=doc['file']
                    ) for doc in files[1:]
                ])
            else:
                # Для других типов отправляем текст отдельно
                await self._execute_with_retry(
                    lambda: self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML',
                        disable_notification=silent,
                        message_thread_id=message_thread_id if message_thread_id else None
                    ),
                    "send_message"
                )
                return await self._send_media_group(chat_id, message_thread_id, media_type, files, silent)

            await self._execute_with_retry(
                lambda: self.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    disable_notification=silent,
                    message_thread_id=message_thread_id if message_thread_id else None
                ),
                "send_media_group"
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to send media group: {str(e)}")
            # Если не удалось отправить группой, отправляем текст и файлы по отдельности
            await self._execute_with_retry(
                lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode='HTML',
                    disable_notification=silent,
                    message_thread_id=message_thread_id if message_thread_id else None
                ),
                "send_message"
            )
            return await self._send_files_individually(chat_id, message_thread_id, media_type, files, silent)

    async def _send_media_group(self, chat_id: str, message_thread_id: Optional[str],
                               media_type: str, files: List[Dict], silent: bool = False) -> bool:
        """Отправляет группу медиафайлов"""
        try:
            if media_type == 'photo':
                media_group = [InputMediaPhoto(media=img['file']) for img in files]
            elif media_type == 'video':
                media_group = [InputMediaVideo(media=vid['file']) for vid in files]
            elif media_type == 'document':
                media_group = [InputMediaDocument(media=doc['file']) for doc in files]
            else:
                return False

            await self._execute_with_retry(
                lambda: self.bot.send_media_group(
                    chat_id=chat_id,
                    media=media_group,
                    disable_notification=silent,
                    message_thread_id=message_thread_id if message_thread_id else None
                ),
                "send_media_group"
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to send media group: {str(e)}")
            return await self._send_files_individually(chat_id, message_thread_id, media_type, files, silent)

    async def _send_files_individually(self, chat_id: str, message_thread_id: Optional[str],
                                      media_type: str, files: List[Dict], silent: bool = False) -> bool:
        """Отправляет файлы по одному"""
        success = True
        for file in files:
            try:
                if not await self._send_media(chat_id, message_thread_id, media_type, file, None, silent):
                    success = False
            except Exception as e:
                self.logger.error(f"Failed to send individual file: {str(e)}")
                success = False
        return success

    async def handle_DATA(self, server: SMTP, session: Session,
                         envelope: Envelope) -> str:
        """Handles incoming messages"""
        try:
            is_valid, error_message = await self.validate_envelope(envelope)
            if not is_valid:
                return error_message

            email_message = BytesParser(policy=default).parsebytes(envelope.content)
            parsed_email = self.extract_message_content(email_message)
            
            # Добавляем информацию о клиенте
            client_ip = ''
            if session and hasattr(session, 'peer') and session.peer:
                client_ip = session.peer[0]
                
            host_name = ''
            if session and hasattr(session, 'host_name'):
                host_name = session.host_name

            message_dict = {
                'from': parsed_email['from'],
                'to': parsed_email['to'],
                'subject': parsed_email['subject'],
                'date': parsed_email['date'],
                'text_body': parsed_email['text_body'],
                'html_body': parsed_email['html_body'],
                'attachments': parsed_email['attachments'],
                'mail_from': envelope.mail_from,
                'rcpt_tos': envelope.rcpt_tos.copy(),
                'X-Client-IP': client_ip or '',
                'X-Host-Name': host_name or '',
                'is_local_delivery': False
            }

            # Update statistics about the message
            self.stats.record_message(client_ip, envelope.rcpt_tos)
            
            # Проверяем получателей а принадлежность к локальному домену
            has_local_recipients = any(self._is_local_recipient(rcpt) for rcpt in envelope.rcpt_tos)
            
            if has_local_recipients:
                self._handle_local_delivery(message_dict)
                # Отправляем сообщения в Telegram для каждого локального получателя
                for recipient in message_dict['local_recipients']:
                    success = await self.send_to_telegram(
                        chat_id=recipient['chat_id'],
                        message_thread_id=recipient['message_thread_id'],
                        message_dict=message_dict,
                        silent=recipient.get('silent', False)
                    )
                    self.stats.record_delivery(success)
                    if not success:
                        self.logger.warning(
                            f"Failed to deliver message to Telegram chat {recipient['chat_id']}"
                        )

            self.messages.append(message_dict)
            
            self.logger.info(
                f'Message accepted from {client_ip or "unknown"} '
                f'with {len(parsed_email["attachments"])} attachments'
                f'{" (local delivery)" if has_local_recipients else ""}'
            )
            return '250 Message accepted for delivery'

        except Exception as e:
            self.logger.error(f'Error processing message: {e}', exc_info=True)
            return '451 Requested action aborted: local error in processing'

    async def handle_QUIT(self, server: SMTP, session: Session,
                         envelope: Envelope) -> str:
        """Handles client disconnect"""
        client_ip = 'unknown'
        if session and hasattr(session, 'peer') and session.peer:
            client_ip = session.peer[0]
        self.logger.info(f'Client disconnected: {client_ip}')
        return '221 Bye'

async def start_server(config: ServerConfig) -> aiosmtpd.controller.Controller:
    """Start SMTP server"""
    handler = CustomSMTPHandler(config)
    controller = aiosmtpd.controller.Controller(
        handler,
        hostname=config.hostname,
        port=config.port
    )
    controller.start()
    handler.start_stats()
    return controller

async def main() -> None:
    """Main function to start the server"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    config = ServerConfig()
    server = None
    
    try:
        server = await start_server(config)
        logger.info(f'SMTP Server started on {config.hostname}:{config.port}')
        logger.info(f'Handling local domains: {", ".join(config.local_domains)}')
        
        while True:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
    except Exception as e:
        logger.error(f'Server error: {e}', exc_info=True)
        raise
    finally:
        if server is not None:
            await server.handler.send_stats()
            server.handler.stop_stats()
            server.stop()
            logger.info('Server stopped')

if __name__ == '__main__':
    asyncio.run(main())
