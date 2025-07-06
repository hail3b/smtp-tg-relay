import pytest
from smtp_server import LocalRecipient

@pytest.mark.parametrize("local_name,chat_id,thread_id,silent", [
    ("-10012345.s", "-10012345", None, True),
    ("-10012345!55.s", "-10012345", "55", True),
    ("id12345", "12345", None, False),
])
def test_local_recipient_parse(local_name, chat_id, thread_id, silent):
    recipient = LocalRecipient.parse(local_name)
    assert recipient is not None
    assert recipient.chat_id == chat_id
    assert recipient.message_thread_id == thread_id
    assert recipient.silent == silent
