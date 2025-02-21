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
    TELEGRAM_BOT_TOKEN
)

from telegram import Bot, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio, InputMediaAnimation
from telegram.error import TelegramError
import html
import mimetypes
from io import BytesIO
from pathlib import Path

@dataclass
class ServerConfig:
    """SMTP Server configuration"""
    hostname: str = SMTP_HOST
    port: int = SMTP_PORT
    max_message_size: int = MAX_MESSAGE_SIZE
    max_stored_messages: int = MAX_STORED_MESSAGES
    local_domains: List[str] = None
    
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
    
    @classmethod
    def parse(cls, local_name: str) -> Optional['LocalRecipient']:
        """Парсит локальное имя и возвращает структуру LocalRecipient"""
        if not local_name:
            return None
            
        parts = local_name.split('!')
            
        if len(parts) == 1:
            parts = local_name.split('_')
                
        # Если только одна часть, предполагается, что это chat_id
        if len(parts) == 1:
            chat_id = parts[0].lstrip("id")  # Убираем префикс "id" если он есть
            return cls(chat_id=chat_id)
        
        # Если две части, предполагается, что это chat_id и message_thread_id
        elif len(parts) == 2:
            chat_id = parts[0].lstrip("id")  # Убираем префикс "id" если он есть
            message_thread_id = parts[1]
            return cls(chat_id=chat_id, message_thread_id=message_thread_id)

        return None

class CustomSMTPHandler:
    """SMTP request handler"""

    def __init__(self, config: ServerConfig):
        self.config = config
        self.messages: deque = deque(maxlen=config.max_stored_messages)
        self.logger = logging.getLogger(__name__)
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)

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
                parsed_email["html_body"] = email_message.get_payload(decode=True).decode()
            else:
                # Если это не текст, обрабатываем как вложение
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
                html_content = part.get_payload(decode=True).decode()
                parsed_email["html_body"] = html_content
                    
                # Добавляем HTML как вложение
                attachment_info = {
                    "filename": "message.html",
                    "content_type": "text/html",
                    "content": html_content.encode("utf-8"),
                    "content_disposition": "attachment",
                    "content_id": "",
                    "size": len(html_content),
                    "encoding": "utf-8",
                    "charset": "utf-8"
                }
                parsed_email["attachments"].append(attachment_info)

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
        """Извлекает и парсит имя локального получателя без домна"""
        _, addr = parseaddr(email)
        for domain in self.config.local_domains:
            if addr.endswith(f"@{domain}"):
                local_name = addr.split(f"@{domain}")[0]
                return LocalRecipient.parse(local_name)
        return None

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
                        'message_thread_id': recipient.message_thread_id
                    })
        
        self.logger.info(f"Processing local delivery for domains: {', '.join(recipient_domains)}")
        message_dict['is_local_delivery'] = True
        message_dict['local_recipient_domains'] = list(recipient_domains)
        message_dict['local_recipients'] = local_recipients

    async def send_to_telegram(self, chat_id: str, message_thread_id: Optional[str], message_dict: Dict) -> bool:
        """Отправляет сообщение в Telegram"""
        try:
            # Формируем текст сообщения
            text = f"📧 <b>Новое email сообщение</b>\n\n"
            text += f"<b>От:</b> {html.escape(message_dict['from'])}\n"
            text += f"<b>Тема:</b> {html.escape(message_dict['subject'])}\n\n"
            
            if message_dict['text_body']:
                text += f"{html.escape(message_dict['text_body'][:4000])}..."
            elif message_dict['html_body']:
                clean_text = re.sub(r'<[^>]+>', '', message_dict['html_body'])
                text += f"{html.escape(clean_text[:4000])}..."

            # Если нет вложений, отправляем только текст
            if not message_dict['attachments']:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode='HTML',
                    message_thread_id=message_thread_id if message_thread_id else None
                )
                return True

            # Группируем вложения по типу
            MEDIA_TYPES = {
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
                },
                'animation': {
                    'image/gif'
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
            
            for attachment in message_dict['attachments']:
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

            # Проверяем, все ли файлы одного типа
            non_empty_types = [(type_name, files) for type_name, files in media_files.items() if files]
            if len(non_empty_types) == 1:
                media_type, files = non_empty_types[0]
                
                # Если файл один, отправляем его с текстом
                if len(files) == 1:
                    file = files[0]
                    file['file'].seek(0)
                    
                    try:
                        if media_type == 'photo':
                            await self.bot.send_photo(
                                chat_id=chat_id,
                                photo=file['file'],
                                caption=text,
                                parse_mode='HTML',
                                message_thread_id=message_thread_id if message_thread_id else None
                            )
                        elif media_type == 'video':
                            await self.bot.send_video(
                                chat_id=chat_id,
                                video=file['file'],
                                caption=text,
                                parse_mode='HTML',
                                message_thread_id=message_thread_id if message_thread_id else None
                            )
                        elif media_type == 'audio':
                            await self.bot.send_audio(
                                chat_id=chat_id,
                                audio=file['file'],
                                caption=text,
                                parse_mode='HTML',
                                message_thread_id=message_thread_id if message_thread_id else None
                            )
                        elif media_type == 'animation':
                            await self.bot.send_animation(
                                chat_id=chat_id,
                                animation=file['file'],
                                caption=text,
                                parse_mode='HTML',
                                message_thread_id=message_thread_id if message_thread_id else None
                            )
                        else:  # document
                            await self.bot.send_document(
                                chat_id=chat_id,
                                document=file['file'],
                                caption=text,
                                parse_mode='HTML',
                                message_thread_id=message_thread_id if message_thread_id else None
                            )
                    finally:
                        file['file'].close()
                    
                    return True
                
                # Если несколько файлов одного типа, отправляем их группой с текстом в первом файле
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
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode='HTML',
                            message_thread_id=message_thread_id if message_thread_id else None
                        )
                        return await self._send_media_group(chat_id, message_thread_id, media_type, files)

                    await self.bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group,
                        message_thread_id=message_thread_id if message_thread_id else None
                    )
                    return True

                except Exception as e:
                    self.logger.error(f"Failed to send media group: {str(e)}")
                    # Если не удалось отправить группой, отправляем текст и файлы по отдельности
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML',
                        message_thread_id=message_thread_id if message_thread_id else None
                    )
                    return await self._send_files_individually(chat_id, message_thread_id, media_type, files)

            # Если файлы разных типов, отправляем текст и группы файлов отдельно
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='HTML',
                message_thread_id=message_thread_id if message_thread_id else None
            )
            
            # Отправляем каждую группу файлов
            for media_type, files in media_files.items():
                if files:
                    await self._send_media_group(chat_id, message_thread_id, media_type, files)

            return True

        except Exception as e:
            self.logger.error(f"Error in send_to_telegram: {str(e)}", exc_info=True)
            return False

    async def _send_media_group(self, chat_id: str, message_thread_id: Optional[str], media_type: str, files: List[Dict]) -> bool:
        """Вспомогательный метод для отправки группы медиафайлов"""
        try:
            if media_type == 'photo':
                media_group = [InputMediaPhoto(media=img['file']) for img in files]
            elif media_type == 'video':
                media_group = [InputMediaVideo(media=vid['file']) for vid in files]
            elif media_type == 'document':
                media_group = [InputMediaDocument(media=doc['file']) for doc in files]
            else:
                return False

            await self.bot.send_media_group(
                chat_id=chat_id,
                media=media_group,
                message_thread_id=message_thread_id if message_thread_id else None
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to send media group: {str(e)}")
            return await self._send_files_individually(chat_id, message_thread_id, media_type, files)

    async def _send_files_individually(self, chat_id: str, message_thread_id: Optional[str], media_type: str, files: List[Dict]) -> bool:
        """Вспомогательный метод для отправки файлов по одному"""
        success = True
        for file in files:
            try:
                file['file'].seek(0)
                if media_type == 'photo':
                    await self.bot.send_photo(
                        chat_id=chat_id,
                        photo=file['file'],
                        message_thread_id=message_thread_id if message_thread_id else None
                    )
                elif media_type == 'document':
                    await self.bot.send_document(
                        chat_id=chat_id,
                        document=file['file'],
                        message_thread_id=message_thread_id if message_thread_id else None
                    )
            except Exception as e:
                self.logger.error(f"Failed to send individual file: {str(e)}")
                success = False
            finally:
                file['file'].close()
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
            
            # Проверяем получателей а принадлежность к локальному домену
            has_local_recipients = any(self._is_local_recipient(rcpt) for rcpt in envelope.rcpt_tos)
            
            if has_local_recipients:
                self._handle_local_delivery(message_dict)
                # Отправляем сообщения в Telegram для каждого локального получателя
                for recipient in message_dict['local_recipients']:
                    success = await self.send_to_telegram(
                        chat_id=recipient['chat_id'],
                        message_thread_id=recipient['message_thread_id'],
                        message_dict=message_dict
                    )
                    if not success:
                        self.logger.warning(f"Failed to deliver message to Telegram chat {recipient['chat_id']}")

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
            server.stop()
            logger.info('Server stopped')

if __name__ == '__main__':
    asyncio.run(main())
