import os
import unicodedata
import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageSendMessage

app = Flask(__name__)

# 環境変数からLINE APIの認証情報を取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Apps Scriptのエンドポイント
GAS_DB_URL = os.getenv("GAS_DB_URL")

# ユーザーの状態を管理する辞書
user_states = {}


# GASから問題リストを取得
def fetch_questions():
    """Google Apps Scriptから問題リストを取得"""
    params = {
        "sheetName": "questions"
    }
    try:
        response = requests.get(GAS_DB_URL, params=params)
        response.raise_for_status()  # Raise HTTPError if not 200 OK
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching questions: {e}")
        return {}


# 問題リストの取得
questions = {question["問題ID"]: question for question in fetch_questions()}
print(f"Fetched {len(questions)} questions")


def log_answer(user_id, question_id, user_answer, correct_answer, result):
    """ユーザーの解答をデータベースに記録"""
    data = {
        "userId": user_id,
        "questionId": question_id,
        "userAnswer": user_answer,
        "correctAnswer": correct_answer,
        "result": result
    }

    try:
        requests.post(GAS_DB_URL, json=data)
    except Exception as e:
        print(f"GAS送信エラー: {e}")


@app.route("/")
def home():
    """ サーバーの動作確認用エンドポイント """
    return "Running"


@app.route("/callback", methods=["POST"])
def callback():
    """ LINEのWebhookエンドポイント """
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


def normalize_answer(answer):
    """ ユーザーの回答を標準化（全角→半角、ソート、小文字化） """
    answer = unicodedata.normalize("NFKC", answer)  # 全角→半角
    answer = answer.replace(",", "").replace(" ", "")  # 余分な文字を削除
    answer = "".join(sorted(answer))  # 文字を昇順にソート
    return answer.lower()  # 小文字に変換


def send_question(user_id, question_id, reply_token):
    """ 指定された問題を出題する（テキスト＋画像） """
    question = questions[question_id]

    # 問題文と選択肢を送信
    messages = [TextSendMessage(text=f"{question['問題ID']}\n{question['問題文']}\n{question['選択肢']}")]

    # 画像がある場合は送信
    if question["画像URL"].strip():
        for image_url in question["画像URL"].split(","):
            messages.append(ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))

    # ユーザーの状態を更新
    user_states[user_id].update({"question_id": question_id, "step": "waiting_answer"})

    # LINEに送信
    line_bot_api.reply_message(reply_token, messages)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """ ユーザーのメッセージを処理する """
    user_id = event.source.user_id
    message_text = event.message.text.strip()

    # ユーザーの状態を取得（デフォルトは通常モード）
    state = user_states.get(user_id, {"step": "waiting_question"})

    if state["step"] == "waiting_question":
        # ① 問題番号待ち
        question_id = message_text.upper()
        if question_id in questions:
            # 問題開始
            user_states[user_id] = {
                "step": "waiting_answer",
                "question_id": question_id
            }
            send_question(user_id, question_id, event.reply_token)
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="問題番号を入力してください"))

    elif state["step"] == "waiting_answer":
        # ② ユーザー回答待ち
        question = questions[state["question_id"]]
        correct_answer = normalize_answer(question["正解"])
        user_answer = normalize_answer(message_text)
        result = user_answer == correct_answer

        if result or message_text == "ギブアップ":
            # 正解またはギブアップ時
            reply_text = f"{'正解！' if result else '残念！'}\n{question['解説']}\n正答率: {question['正答率']}\nテーマ: {question['テーマ']}\n続けますか？[はい/いいえ]"
            user_states[user_id]["step"] = "waiting_confirmation"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        else:
            # 不正解時
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="違います"))

        # 解答結果記録
        log_answer(user_id, state["question_id"], message_text, correct_answer, result)

    elif state["step"] == "waiting_confirmation":
        if message_text == "いいえ":
            user_states[user_id] = {"step": "waiting_question"}
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="終了します"))
        else:
            # 次の問題へ
            question_ids = list(questions.keys())
            current_index = question_ids.index(state["question_id"]) if state["question_id"] in question_ids else -1
            if current_index + 1 < len(question_ids):
                next_question = question_ids[current_index + 1]
                user_states[user_id] = {
                    "step": "waiting_answer",
                    "question_id": next_question
                }
                send_question(user_id, next_question, event.reply_token)
            else:
                user_states[user_id] = {"step": "waiting_question"}
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="これが最後の問題です"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
