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
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = os.environ['CHAT_ID']

SMTP_SERVER_ADDR = os.environ.get('SMTP_SERVER_ADDR', '')
SMTP_SERVER_PORT = os.environ.get('SMTP_SERVER_PORT', 25)

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

    def apply_watermark(self, io_img, text):

        # Открываем изображение
        img = Image.open(io_img)

        # Создаем объект ImageDraw
        draw = ImageDraw.Draw(img)
        width, height = img.size

        # Определяем размеры прямоугольника
        rectangle_height = 15
        rectangle_width = width

        # Определяем координаты верхнего левого и нижнего правого углов прямоугольника
        rectangle_top_left = (0, height - rectangle_height)
        rectangle_bottom_right = (width, height)
        draw.rectangle([rectangle_top_left, rectangle_bottom_right], fill=(70, 70, 70))

        text = text # f"Камера: CAM001; Дата снимка: 14.10.2023 17:43:02"
        font = ImageFont.truetype("ArialBold_.ttf", 14)
        text_color = (255, 255, 255)
        draw.text((5, img.height - 15), text, fill=text_color, font=font)
        draw.text((6, img.height - 15), text, fill=text_color, font=font)
        output_bytes_io = io.BytesIO()
        img.save(output_bytes_io, format='PNG')
        output_bytes_io.seek(0)
        return output_bytes_io

    def send_photo_to_telegram(self, photo_stream, message_text, reply_to_message_id):
        try:
            logging.info(f'send_photo_to_telegram {message_text=}')
            photo_stream = self.apply_watermark(photo_stream, message_text)
            self.bot.send_photo(chat_id=self.chat_id, photo=photo_stream, reply_to_message_id=reply_to_message_id)
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
                return value, key
        return None, msg["From"]

    async def handle_attachment(self, msg, part, filename, mail_from):
        try:
            file_data = part.get_payload(decode=True)
            photo_stream = io.BytesIO(file_data)
            now = datetime.datetime.now()
            thread_id, thread_name = self.get_thread_id(msg)
            message_text = self.format_message(mail_from, filename, now, thread_name)
            self.telegram_sender.send_photo_to_telegram(photo_stream, message_text, reply_to_message_id=thread_id)
        except Exception as e:
            logging.error(f'Error while handling attachment: {str(e)}')


    @staticmethod
    def format_message(mail_from, filename, now, thread_name):
        return f'Дата снимка: {now.strftime("%Y-%m-%d %H:%M:%S")}; Камера: {thread_name}'
        #return f'Камера: {mail_from}\nНазвание файла: {filename}\nДата снимка: {now.strftime("%Y-%m-%d %H:%M:%S")}'


def main():
    controller = Controller(CustomSMTPHandler(BOT_TOKEN, CHAT_ID), hostname=SMTP_SERVER_ADDR, port=SMTP_SERVER_PORT)
    try:
        controller.start()
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        logging.error(f'Error in main function: {str(e)}')


if __name__ == '__main__':
    main()
