from dotenv import load_dotenv
load_dotenv()

import os
from typing import List

# Server settings
SMTP_HOST = os.getenv('SMTP_HOST', '127.0.0.1')
SMTP_PORT = int(os.getenv('SMTP_PORT', '1025'))
MAX_MESSAGE_SIZE = int(os.getenv('SMTP_MAX_MESSAGE_SIZE', str(100 * 1024 * 1024)))  # 100MB default
MAX_STORED_MESSAGES = int(os.getenv('SMTP_MAX_STORED_MESSAGES', '500'))

# Local domains configuration
# Принимаем домены как строку, разделенную запятыми
DEFAULT_DOMAINS = 'example.com'
LOCAL_DOMAINS_STR = os.getenv('SMTP_LOCAL_DOMAINS', DEFAULT_DOMAINS)
LOCAL_DOMAINS = [domain.strip() for domain in LOCAL_DOMAINS_STR.split(',') if domain.strip()]

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'TEST_TOKEN')

# Statistics settings
STATS_ADMIN_CHAT_ID = os.getenv('STATS_ADMIN_CHAT_ID')
STATS_INTERVAL = int(os.getenv('STATS_INTERVAL', '3600'))

def get_local_domains() -> List[str]:
    """Получить список локальных доменов из переменных окружения"""
    return LOCAL_DOMAINS 
