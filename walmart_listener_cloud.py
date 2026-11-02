import imaplib
import email
from email.header import decode_header
import time
import json
import os
import sys
import hmac
import hashlib
import base64
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

# ==================== 配置区域 ====================
# 所有敏感信息一律从 GitHub Secrets 环境变量读取，代码中不留任何明文
IMAP_SERVER = os.environ.get("IMAP_SERVER", "imap.qq.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
EMAIL_ACCOUNT = os.environ.get("EMAIL_ACCOUNT", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "").strip()

DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "").strip()
DINGTALK_SECRET = os.environ.get("DINGTALK_SECRET", "").strip()

SENDER_FILTER = os.environ.get("SENDER_FILTER", "walmart")
SUBJECT_FILTER = os.environ.get("SUBJECT_FILTER", "Order")
# ==================================================

PROCESSED_IDS_FILE = "processed_emails.txt"


def check_config():
    """启动前校验必要参数是否齐全"""
    missing = []
    for name, val in [
        ("EMAIL_ACCOUNT", EMAIL_ACCOUNT),
        ("EMAIL_PASSWORD", EMAIL_PASSWORD),
        ("DINGTALK_WEBHOOK", DINGTALK_WEBHOOK),
    ]:
        if not val:
            missing.append(name)
    if missing:
        print(f"[配置错误] 缺少环境变量: {', '.join(missing)}")
        print("请在 GitHub 仓库 Settings -> Secrets and variables -> Actions 中补齐。")
        sys.exit(1)


def load_processed_ids():
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_processed_ids(ids_set):
    """只保留最近 2000 条，避免文件无限膨胀"""
    with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
        for item in sorted(ids_set)[-2000:]:
            f.write(f"{item}\n")


def decode_str(header_str):
    if not header_str:
        return ""
    decoded_chunks = decode_header(header_str)
    result = []
    for chunk, charset in decoded_chunks:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="ignore"))
        else:
            result.append(str(chunk))
    return "".join(result)


def get_signed_url():
    if not DINGTALK_SECRET:
        return DINGTALK_WEBHOOK
    timestamp = str(round(time.time() * 1000))
    secret_enc = DINGTALK_SECRET.encode("utf-8")
    string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
    string_to_sign_enc = string_to_sign.encode("utf-8")
    hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"{DINGTALK_WEBHOOK}&timestamp={timestamp}&sign={sign}"


def send_dingtalk_markdown(title, content):
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": content}}
    try:
        url = get_signed_url()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            if res_json.get("errcode") == 0:
                print(f"[OK] 钉钉推送成功: {title}")
                return True
            print(f"[FAIL] 钉钉推送失败: {res_json}")
            return False
    except Exception as e:
        print(f"[ERROR] 钉钉请求异常: {e}")
        return False


def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    return body


def run_once():
    check_config()
    processed_ids = load_processed_ids()
    new_processed = set(processed_ids)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始检查邮箱: {EMAIL_ACCOUNT}")

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("INBOX")

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages[0]:
            print("没有未读邮件。")
            mail.logout()
            return

        email_ids = messages[0].split()
        print(f"发现 {len(email_ids)} 封未读邮件，正在匹配沃尔玛订单...")
        hit = 0

        for e_id in email_ids:
            str_id = e_id.decode("utf-8")
            if str_id in processed_ids:
                continue

            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if res != "OK":
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject = decode_str(msg.get("Subject", ""))
            sender = decode_str(msg.get("From", ""))

            if SENDER_FILTER.lower() in sender.lower() or SUBJECT_FILTER.lower() in subject.lower():
                body = get_email_body(msg)
                bj_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                snippet = body[:300].strip().replace("\n", " ") if body else "无正文预览"

                markdown_text = f"""### 🛒 沃尔玛自发货新订单提醒
---
- **订单主题**：{subject}
- **发件人**：{sender}
- **接收时间**：{bj_time}

> **邮件摘要**：
> {snippet}...

👉 [进入领星自发货订单管理](https://erp.lingxing.com/erp/mmulti/mpOrderManagement)
"""
                send_dingtalk_markdown("【订单提醒】沃尔玛新订单", markdown_text)
                hit += 1

            new_processed.add(str_id)

        save_processed_ids(new_processed)
        mail.logout()
        print(f"检查完成，本次命中沃尔玛订单 {hit} 条。")

    except Exception as e:
        print(f"执行检查异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run_once()
