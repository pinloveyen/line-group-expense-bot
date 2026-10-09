
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
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
).strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get(
    "GEMINI_MODEL", "gemini-3.8-flash"
).strip()
GOOGLE_CREDENTIALS_JSON = os.environ.get(
    "GOOGLE_CREDENTIALS_JSON", ""
).strip()

SHEET_NAME = "記帳紀錄"
SHEET_HEADERS = [
    "紀錄時間", "群組ID", "使用者ID",
    "類別", "項目", "金額", "備註"
]

handler = WebhookHandler(CHANNEL_SECRET)


# ---------- LINE 回覆 ----------

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


def reply_menu(reply_token):
    """在 LINE 群組顯示四個功能按鈕。"""

    menu_contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "群組記帳助手",
                    "weight": "bold",
                    "size": "xl",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": "請選擇要使用的功能",
                    "size": "sm",
                    "color": "#666666",
                    "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "action": {
                        "type": "message",
                        "label": "新增支出",
                        "text": "新增支出",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "message",
                        "label": "查詢總額",
                        "text": "查詢總額",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "message",
                        "label": "今日支出",
                        "text": "今日支出",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "message",
                        "label": "支出分類",
                        "text": "支出分類",
                    },
                },
            ],
        },
    }

    configuration = Configuration(
        access_token=CHANNEL_ACCESS_TOKEN
    )

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[
                    FlexMessage(
                        alt_text="群組記帳助手功能選單",
                        contents=FlexContainer.from_dict(
                            menu_contents
                        ),
                    )
                ],
            )
        )


# ---------- 快速解析與 Gemini 解析 ----------

def fast_parse_expense(message_text):
    """
    解析簡單格式，例如：
    早餐 100
    咖啡65
    午餐 120元

    無法確定格式時回傳 None，交給 Gemini 處理。
    """

    match = re.fullmatch(
        r"\s*(?P<item>[\u4e00-\u9fffA-Za-z]"
        r"[\u4e00-\u9fffA-Za-z0-9 _-]{0,29}?)"
        r"\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
        r"\s*(?:元|塊錢|塊)?\s*",
        message_text,
    )

    if not match:
        return None

    item = match.group("item").strip()
    amount_text = match.group("amount").replace(",", "")

    try:
        amount = float(amount_text)
    except ValueError:
        return None

    if not item or not math.isfinite(amount) or amount <= 0:
        return None

    if any(word in item for word in (
        "早餐", "午餐", "晚餐", "宵夜",
        "咖啡", "飲料", "便當", "吃飯",
    )):
        category = "餐飲"
    elif any(word in item for word in (
        "捷運", "公車", "火車", "計程車",
        "加油", "停車",
    )):
        category = "交通"
    elif any(word in item for word in (
        "衣服", "購物", "網購", "鞋子",
    )):
        category = "購物"
    else:
        category = "其他"

    return {
        "is_expense": True,
        "item": item,
        "amount": amount,
        "category": category,
        "note": "",
    }


def parse_expense(message_text):
    # 優先使用快速解析，減少簡單記帳的 AI 等待時間
    result = fast_parse_expense(message_text)

    if result is not None:
        logging.info("使用快速規則解析記帳")
        return result

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 未設定")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
你是記帳資料解析助手。請只回傳一個合法 JSON 物件，
不要使用 Markdown，也不要加入其他說明。

欄位：
- is_expense：是否能確認為支出，布林值
- item：支出項目，字串
- amount：金額，正數數字；無法確認時填 null
- category：餐飲、交通、購物、娛樂、生活或其他
- note：備註，沒有就填空字串

若不是支出，或無法確認金額，請設定 is_expense 為 false，
amount 為 null。不可自行猜測金額。

使用者訊息：
{message_text}
"""

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
                "Gemini 暫時性錯誤，%s 秒後重試",
                wait_seconds,
            )
            time.sleep(wait_seconds)

    raw_text = (getattr(response, "text", None) or "").strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text).strip()

    start = raw_text.find("{")
    end = raw_text.rfind("}")

    if start < 0 or end < start:
        raise ValueError("Gemini 沒有回傳有效的 JSON")

    result = json.loads(raw_text[start:end + 1])

    if not isinstance(result, dict):
        raise ValueError("Gemini 回傳格式錯誤")

    if not isinstance(result.get("is_expense"), bool):
        raise ValueError("is_expense 欄位格式錯誤")

    if result["is_expense"]:
        amount = result.get("amount")

        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise ValueError("支出金額格式錯誤")

    return result


# ---------- Google 試算表 ----------

def get_spreadsheet_id():
    raw = os.environ.get("GOOGLE_SHEET_ID", "").strip()

    if not raw:
        raise RuntimeError("GOOGLE_SHEET_ID 未設定")

    # 支援完整 Google 試算表網址
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)

    if match:
        return match.group(1)

    # 支援純 ID 或 ID/edit?gid=... 格式
    match = re.fullmatch(
        r"([A-Za-z0-9_-]+)(?:/edit(?:\?.*)?)?",
        raw,
    )

    if match:
        return match.group(1)

    raise RuntimeError("GOOGLE_SHEET_ID 格式不正確")


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

    if not worksheet.row_values(1):
        worksheet.append_row(SHEET_HEADERS)

    source = event.source
    group_id = getattr(source, "group_id", "") or ""
    user_id = getattr(source, "user_id", "") or ""

    recorded_at = datetime.now(
        ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")

    amount = expense.get("amount")

    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(amount)
        or amount <= 0
    ):
        raise ValueError("支出金額必須是有效的正數")

    row = [
        recorded_at,
        group_id,
        user_id,
        str(expense.get("category") or "其他"),
        str(expense.get("item") or "未命名項目"),
        amount,
        str(expense.get("note") or ""),
    ]

    worksheet.append_row(
        row,
        value_input_option="USER_ENTERED",
    )

    logging.info("記帳資料已寫入 Google 試算表")


# ---------- Flask 與 LINE Webhook ----------

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

    # 顯示群組功能選單
    if message_text in ("選單", "功能", "記帳選單"):
        try:
            reply_menu(event.reply_token)
        except Exception:
            logging.exception("功能選單發送失敗")
        return

    # 新增支出：提示使用者輸入項目與金額
    if message_text == "新增支出":
        reply_text(
            event.reply_token,
            "請輸入支出項目與金額，例如：早餐 100",
        )
        return

    # 查詢功能先接通按鈕，統計功能下一階段實作
    if message_text in ("查詢總額", "今日支出", "支出分類"):
        reply_text(
            event.reply_token,
            "已收到你的功能選擇。此查詢功能尚未啟用，"
            "目前不會顯示未經計算的金額。",
        )
        return

    try:
        expense = parse_expense(message_text)

        if not expense.get("is_expense"):
            reply_text(
                event.reply_token,
                "我還無法確認這是一筆支出。請試著輸入：早餐 100",
            )
            return

        # 先確認試算表寫入成功，再回覆成功
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
