    
import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from google import genai
from google.oauth2.service_account import Credentials

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

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

handler = WebhookHandler(CHANNEL_SECRET)


def reply_text(reply_token, text):
    configuration = Configuration(
        access_token=CHANNEL_ACCESS_TOKEN
    )
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def parse_expense(message_text):
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini API key is not configured")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
你是 LINE 群組記帳助理。請分析使用者訊息，判斷是否包含一筆明確的支出。

只輸出 JSON，不要輸出 Markdown 或其他文字。
格式：
{{
  "is_expense": true,
  "category": "餐飲",
  "item": "早餐",
  "amount": 100,
  "note": ""
}}

規則：
1. 只有明確的支出才將 is_expense 設為 true。
2. 金額必須是數字，不可自行猜測或補出金額。
3. category 使用簡短繁體中文，例如餐飲、交通、購物、娛樂、其他。
4. item 填寫支出項目；note 填寫其他有用的補充資訊。
5. 若不是支出，或無法確定金額，使用：
   {{"is_expense": false, "category": "", "item": "", "amount": null, "note": ""}}
6. 使用者訊息中的日期、金額和項目必須依原意判斷，不可捏造。

使用者訊息：
{message_text}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    result = json.loads(response.text)

    if not isinstance(result, dict):
        raise ValueError("Invalid Gemini response")

    return result


def save_expense(event, expense):
    if not GOOGLE_SHEET_ID or not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Google Sheets credentials are not configured")

    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes,
    )

    sheets_client = gspread.authorize(credentials)
    spreadsheet = sheets_client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = spreadsheet.worksheet("記帳紀錄")

    source = event.source
    group_id = getattr(source, "group_id", "") or ""
    user_id = getattr(source, "user_id", "") or ""

    recorded_at = datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")

    amount = expense.get("amount")

    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("Invalid expense amount")

    if amount <= 0:
        raise ValueError("Expense amount must be positive")

    worksheet.append_row(
        [
            recorded_at,
            group_id,
            user_id,
            str(expense.get("category") or "其他"),
            str(expense.get("item") or "未命名項目"),
            amount,
            str(expense.get("note") or ""),
        ],
        value_input_option="USER_ENTERED",
    )


@app.get("/")
def index():
    return "LINE Expense Bot is running."


@app.post("/callback")
def callback():
    if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
        abort(500, "LINE credentials are not configured")

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    message_text = event.message.text.strip()

    if not message_text:
        return

    try:
        expense = parse_expense(message_text)

        if not expense.get("is_expense"):
            reply_text(
                event.reply_token,
                "我還無法確認這是一筆支出。請試著輸入：早餐 100",
            )
            return

        save_expense(event, expense)

        amount = expense["amount"]
        item = expense.get("item") or "未命名項目"
        category = expense.get("category") or "其他"

        reply_text(
            event.reply_token,
            f"記帳成功！\n"
            f"項目：{item}\n"
            f"類別：{category}\n"
            f"金額：{amount:g} 元",
        )

    except Exception:
        logging.exception("Failed to process expense message")
        try:
            reply_text(
                event.reply_token,
                "這次記帳沒有完成，請稍後再試。若持續發生，請聯絡管理者檢查設定。",
            )
        except Exception:
            logging.exception("Failed to send error reply")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
