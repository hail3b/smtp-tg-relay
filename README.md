# smtp-tg-relay

This project forwards e-mails to Telegram chats using a bot. The recipient address encodes the Telegram chat ID and optional thread ID.

## Recipient address format

```
<chat_id>[!<thread_id>][.<flags>]@<local_domain>
```

Example: `-1001234567890!55.s@example.com`

Available flags:

- `s` – send the message in *silent* mode (without notification sound).

## Running tests

Install requirements and run `pytest`:

```bash
pip install -r requirements.txt -r requirements-test.txt
PYTHONPATH=. pytest
```
