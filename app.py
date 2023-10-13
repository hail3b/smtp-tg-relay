"""
Асинхронный SMTP-сервер, который обрабатывает входящие электронные письма и отправляет их содержимое в Telegram.

Классы:
    TelegramSender: Отправляет содержимое писем в формате изображений в Telegram.
    CustomSMTPHandler: Обрабатывает входящие сообщения SMTP, извлекает вложения и отправляет их содержимое в Telegram.

Глобальные переменные:
    BOT_TOKEN (str): Токен бота для доступа к Telegram API.
    CHAT_ID (str): Идентификатор чата Telegram, в который будут отправляться сообщения.
    SMTP_SERVER_ADDR (str): IP адрес SMTP-сервера.
    SMTP_SERVER_PORT (int): Порт, используемый для запуска SMTP-сервера.

Функции:
    main: Инициализирует и запускает контроллер SMTP-сервера для обработки входящих сообщений.
"""


import asyncio
import datetime
import io
import email
import logging
import os

from aiosmtpd.controller import Controller
from telegram import Bot

BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = os.environ['CHAT_ID']

SMTP_SERVER_ADDR = ''
SMTP_SERVER_PORT = 25

cameras = {
    'CAM001': 2,
    'CAM002': 3,
}
serialized_cameras = str(cameras)


logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)

class TelegramSender:
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.bot = Bot(token=self.bot_token)

    async def send_photo_to_telegram(self, photo_stream, message_text, reply_to_message_id):
        try:
            logging.info(f'send_photo_to_telegram {message_text=}')
            self.bot.send_photo(chat_id=self.chat_id, photo=photo_stream, caption=message_text, reply_to_message_id=reply_to_message_id)
        except Exception as e:
            logging.error(f'Failed to send photo to Telegram. Error: {str(e)}')

class CustomSMTPHandler:
    def __init__(self, bot_token, chat_id):
        self.telegram_sender = TelegramSender(bot_token, chat_id)

    async def handle_DATA(self, server, session, envelope):
        try:
            msg = email.message_from_bytes(envelope.original_content)
            mail_from = msg["From"]
            logging.info('1 new message, messages processing ...')
            await self.process_message_parts(msg, mail_from)
            logging.info('ok')
            return '250 Message accepted for delivery'
        except Exception as e:
            logging.error(f'Error while processing message: {str(e)}')
            return '451 Requested action aborted: error in processing'

    async def process_message_parts(self, msg, mail_from):
        try:
            for part in msg.walk():
                if self.is_valid_part(part):
                    filename = part.get_filename()
                    if filename:
                        await self.handle_attachment(msg, part, filename, mail_from)
        except Exception as e:
            logging.error(f'Error while processing message parts: {str(e)}')

    def is_valid_part(self, part):
        return part.get_content_maintype() != 'multipart' and part.get('Content-Disposition') is not None

    def get_thread_id(self, msg):
        cameras = {
            'CAM001': 2,
            'CAM002': 3,
        }
        for key, value in cameras.items():
            if key in msg["From"]:
                return value
        return None

    async def handle_attachment(self, msg, part, filename, mail_from):
        try:
            file_data = part.get_payload(decode=True)
            photo_stream = io.BytesIO(file_data)
            now = datetime.datetime.now()
            message_text = self.format_message(mail_from, filename, now)
            thread_id = self.get_thread_id(msg)
            await self.telegram_sender.send_photo_to_telegram(photo_stream, message_text, reply_to_message_id=thread_id)
        except Exception as e:
            logging.error(f'Error while handling attachment: {str(e)}')

    @staticmethod
    def format_message(mail_from, filename, now):
        #return f'Камера: {mail_from}\nНазвание файла: {filename}\nДата снимка: {now.strftime("%Y-%m-%d %H:%M:%S")}'
        return f'Дата снимка: {now.strftime("%Y-%m-%d %H:%M:%S")}'


def main():
    controller = Controller(CustomSMTPHandler(BOT_TOKEN, CHAT_ID), hostname=SMTP_SERVER_ADDR, port=SMTP_SERVER_PORT)
    try:
        controller.start()
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logging.error(f'Error in main function: {str(e)}')


if __name__ == '__main__':
    main()
