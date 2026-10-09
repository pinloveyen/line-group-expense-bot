
import os
import json
import logging
import math
import re
import time
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()

SHEET_NAME = "記帳紀錄"
SHEET_HEADERS = ["紀錄時間", "群組ID", "使用者ID", "類別", "項目", "金額", "備註"]

handler = WebhookHandler(CHANNEL_SECRET)


def get_spreadsheet_id():
    """接受純 ID、試算表網址，或 ID 後面帶 /edit 的常見格式。"""
    raw = os.environ.get("GOOGLE_SHEET_ID", "").strip()

    if not raw:
        raise RuntimeError("GOOGLE_SHEET_ID 未設定")

    # 完整網址：擷取 /spreadsheets/d/ 後的 ID
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if match:
        return match.group(1)

    # 純 ID，或 ID 後面誤帶 /edit?gid=...
    match = re.fullmatch(
        r"([A-Za-z0-9_-]+)(?:/edit(?:\?.*)?)?",
        raw,
    )
    if match:
        return match.group(1)

    raise RuntimeError(
        "GOOGLE_SHEET_ID 格式不正確，請填入 Google 試算表 ID 或網址"
    )


def reply_text(reply_token, text):
    configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
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
        raise RuntimeError("GEMINI_API_KEY 未設定")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
你是記帳資料解析助手。請分析以下訊息，且只回傳一個合法 JSON 物件，
不要使用 Markdown，不要加上任何說明。

欄位：
- is_expense：是否能確認為一筆支出，布林值
- item：支出項目，字串
- amount：金額，正數數字；無法確認時填 null
- category：類別，例如餐飲、交通、購物、娛樂、生活、其他
- note：補充備註，沒有就填空字串

若不是支出、只是聊天，或無法確認金額，請設定 is_expense 為 false，
amount 為 null。不要自行猜測金額。

使用者訊息：
{message_text}
"""

    # 只對暫時性服務錯誤重試，最多 3 次
    response = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            break
        except Exception as exc:
            error_text = str(exc).lower()
            status_code = getattr(exc, "code", None)
            retryable = (
                status_code in (429, 500, 502, 503, 504)
                or any(code in error_text for code in (
                    "429", "500", "502", "503", "504",
                    "unavailable", "high demand", "temporarily",
                ))
            )

            if not retryable or attempt == 2:
                logging.exception("Gemini 請求失敗")
                raise

            wait_seconds = attempt + 1
            logging.warning(
                "Gemini 暫時性錯誤，將於 %s 秒後重試（第 %s 次）",
                wait_seconds,
                attempt + 1,
            )
            time.sleep(wait_seconds)

    raw_text = (getattr(response, "text", None) or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    # 容忍模型在 JSON 前後多出少量文字
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Gemini 沒有回傳有效的 JSON 物件")

    result = json.loads(raw_text[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Gemini 回傳格式不是 JSON 物件")

    if not isinstance(result.get("is_expense"), bool):
        raise ValueError("Gemini 回傳的 is_expense 欄位格式錯誤")

    if result["is_expense"]:
        amount = result.get("amount")

        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError("支出金額不是有效數字")

        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("支出金額必須是有限的正數")

    return result


def save_expense(event, expense):
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON 未設定")

    spreadsheet_id = get_spreadsheet_id()

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

    # 日誌只記錄 ID 長度，不輸出完整 ID 或憑證
    logging.info(
        "正在開啟 Google 試算表，ID 長度：%s",
        len(spreadsheet_id),
    )

    spreadsheet = sheets_client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=SHEET_NAME,
            rows="1000",
            cols=str(len(SHEET_HEADERS)),
        )
        worksheet.append_row(SHEET_HEADERS)
        logging.info("已建立工作表：%s", SHEET_NAME)

    # 工作表存在但完全空白時，補上標題列
    if not worksheet.row_values(1):
        worksheet.append_row(SHEET_HEADERS)

    source = event.source
    group_id = getattr(source, "group_id", "") or ""
    user_id = getattr(source, "user_id", "") or ""
    recorded_at = datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")

    amount = expense.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError("支出金額格式錯誤")
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("支出金額必須是有限的正數")

    row = [
        recorded_at,
        group_id,
        user_id,
        str(expense.get("category") or "其他"),
        str(expense.get("item") or "未命名項目"),
        amount,
        str(expense.get("note") or ""),
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logging.info("記帳資料已寫入 Google 試算表")


@app.get("/")
def index():
    return "LINE Expense Bot is running."


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/callback")
def callback():
    if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
        logging.error("LINE 憑證尚未完整設定")
        abort(500, "LINE credentials are not configured")

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logging.warning("LINE Webhook 簽章驗證失敗")
        abort(400, "Invalid signature")

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    message_text = (event.message.text or "").strip()
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
            f"記帳成功！\n項目：{item}\n類別：{category}\n金額：{amount:g} 元",
        )

    except Exception:
        # 不記錄使用者完整訊息、API 金鑰或服務帳戶憑證
        logging.exception("記帳處理失敗")

        try:
            reply_text(
                event.reply_token,
                "這次記帳沒有完成，請稍後再試。"
                "若持續發生，請管理者檢查 Render 日誌。",
            )
        except Exception:
            logging.exception("LINE 錯誤回覆發送失敗")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
