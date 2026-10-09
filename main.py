
import os
import hmac
import hashlib
import base64

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None


@app.get("/")
def index():
    return "LINE Expense Bot is running."


@app.post("/callback")
def callback():
    if not handler or not CHANNEL_ACCESS_TOKEN:
        abort(500, "LINE credentials are not configured")

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent) if handler else (lambda f: f)
def handle_text_message(event):
    if not CHANNEL_ACCESS_TOKEN:
        return

    text = event.message.text.strip()

    reply_text = (
        f"收到你的訊息：{text}\n"
        "記帳功能正在建置中，完成後就能解析項目與金額。"
    )

    configuration = Configuration(
        access_token=CHANNEL_ACCESS_TOKEN
    )

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
