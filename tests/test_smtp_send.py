import pytest
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path
import os
from config import SMTP_HOST, SMTP_PORT
from smtp_server import ServerConfig, start_server
from PIL import Image
import io

@pytest.fixture(scope="session")
async def smtp_server():
    """Фикстура для запуска SMTP сервера"""
    config = ServerConfig()
    server = await start_server(config)
    yield server
    server.stop()

@pytest.fixture(scope="session")
def event_loop():
    """Создает экземпляр event loop для асинхронных фикстур"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture
def smtp_client(smtp_server):
    """Фикстура для создания SMTP клиента"""
    client = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
    yield client
    client.quit()

def create_test_message(
    sender: str = "sender@example.com",
    recipient: str = "-1002174188126!60@s.roskar.ru",
    subject: str = "Тестовое сообщение",
    body: str = "Это тестовое сообщение"
) -> MIMEMultipart:
    """Создает тестовое сообщение"""
    message = MIMEMultipart()
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = subject

    # Добавляем текстовую часть
    text_part = MIMEText(body, 'plain')
    message.attach(text_part)

    return message

def test_send_simple_message(smtp_client):
    """Тест отправки простого текстового сообщения"""
    message = create_test_message()
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения: {e}")

def test_send_message_with_attachment(smtp_client):
    """Тест отправки сообщения с вложением"""
    message = create_test_message(
        subject="Тестовое сообщение с вложением",
        body="Это тестовое сообщение с текстовым файлом во вложении"
    )
    
    # Создаем тестовый файл
    test_file_content = "Это содержимое тестового файла\nВторая строка\nТретья строка"
    attachment = MIMEApplication(test_file_content.encode('utf-8'))
    attachment.add_header('Content-Disposition', 'attachment', filename='test.txt')
    message.attach(attachment)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение с вложением должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения с вложением: {e}")

def test_send_large_message(smtp_client):
    """Тест отправки большого сообщения"""
    large_body = "A" * (1024 * 1024 * 2)  # 2MB текст
    message = create_test_message(body=large_body)
    
    with pytest.raises(smtplib.SMTPResponseException):
        smtp_client.send_message(message)

def test_send_html_message(smtp_client):
    """Тест отправки HTML сообщения"""
    message = MIMEMultipart('alternative')
    message['From'] = "test@example.com"
    message['To'] = "-1002174188126!60@s.roskar.ru"
    message['Subject'] = "HTML Тест"
    
    html = """
    <html>
        <body>
            <h1>Тестовое HTML сообщение</h1>
            <p>Это <b>жирный</b> текст</p>
        </body>
    </html>
    """
    
    html_part = MIMEText(html, 'html')
    message.attach(html_part)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "HTML сообщение должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке HTML сообщения: {e}")

def test_send_message_with_image(smtp_client):
    """Тест отправки сообщения с изображением"""
    message = create_test_message(subject="Тестовое сообщение с изображением")
    
    # Создаем реальное тестовое изображение
    img = Image.new('RGB', (100, 100), color = 'red')
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr = img_byte_arr.getvalue()
    
    # Прикрепляем изображение
    image_attachment = MIMEApplication(img_byte_arr, _subtype="png")
    image_attachment.add_header('Content-Disposition', 'attachment', filename='test_image.png')
    message.attach(image_attachment)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение с изображением должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения с изображением: {e}")

def test_send_message_with_multiple_images(smtp_client):
    """Тест отправки сообщения с несколькими изображениями"""
    message = create_test_message(subject="Тестовое сообщение с несколькими изображениями")
    
    # Создаем несколько тестовых изображений разных цветов
    colors = ['red', 'blue', 'green']
    for i, color in enumerate(colors):
        # Создаем изображение
        img = Image.new('RGB', (100, 100), color=color)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='PNG')
        img_byte_arr = img_byte_arr.getvalue()
        
        # Прикрепляем изображение
        image_attachment = MIMEApplication(img_byte_arr, _subtype="png")
        image_attachment.add_header(
            'Content-Disposition', 
            'attachment', 
            filename=f'test_image_{i+1}_{color}.png'
        )
        message.attach(image_attachment)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение с несколькими изображениями должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения с несколькими изображениями: {e}")

def test_send_message_with_multiple_documents(smtp_client):
    """Тест отправки сообщения с несколькими документами"""
    message = create_test_message(subject="Тестовое сообщение с несколькими документами")
    
    # Создаем несколько тестовых текстовых документов
    for i in range(3):
        # Создаем текстовый контент
        text_content = f"Это содержимое текстового файла номер {i+1}\n"
        text_content += f"Тестовая строка 1\n"
        text_content += f"Тестовая строка 2\n"
        text_content += f"Тестовая строка 3"
        
        # Прикрепляем документ
        doc_attachment = MIMEApplication(text_content.encode('utf-8'), _subtype="txt")
        doc_attachment.add_header(
            'Content-Disposition', 
            'attachment', 
            filename=f'test_document_{i+1}.txt'
        )
        message.attach(doc_attachment)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение с несколькими документами должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения с несколькими документами: {e}")

def test_send_message_with_mixed_attachments(smtp_client):
    """Тест отправки сообщения с разными типами вложений"""
    message = create_test_message(subject="Тестовое сообщение с разными вложениями")
    
    # Добавляем изображение
    img = Image.new('RGB', (100, 100), color='red')
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr = img_byte_arr.getvalue()
    
    image_attachment = MIMEApplication(img_byte_arr, _subtype="png")
    image_attachment.add_header(
        'Content-Disposition', 
        'attachment', 
        filename='test_image.png'
    )
    message.attach(image_attachment)
    
    # Добавляем текстовый файл 1
    text_content1 = "Это первый текстовый файл\nС несколькими строками\nТекста"
    text_attachment1 = MIMEApplication(text_content1.encode('utf-8'), _subtype="txt")
    text_attachment1.add_header(
        'Content-Disposition', 
        'attachment', 
        filename='test_document1.txt'
    )
    message.attach(text_attachment1)
    
    # Добавляем текстовый файл 2
    text_content2 = "Это второй текстовый файл\nТоже с несколькими строками\nТекста"
    text_attachment2 = MIMEApplication(text_content2.encode('utf-8'), _subtype="txt")
    text_attachment2.add_header(
        'Content-Disposition', 
        'attachment', 
        filename='test_document2.txt'
    )
    message.attach(text_attachment2)
    
    try:
        result = smtp_client.send_message(message)
        assert result == {}, "Сообщение с разными типами вложений должно быть отправлено успешно"
    except smtplib.SMTPException as e:
        pytest.fail(f"Ошибка при отправке сообщения с разными типами вложений: {e}")

if __name__ == '__main__':
    pytest.main([__file__]) 