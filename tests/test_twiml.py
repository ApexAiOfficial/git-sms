from main import twilio_reply

def test_twiml_escapes_message_content():
    response = twilio_reply('<tag attr="x">&')
    assert response.status_code == 200
    assert "&lt;tag attr=&quot;x&quot;&gt;&amp;" in response.body.decode()
