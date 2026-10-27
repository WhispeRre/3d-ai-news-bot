#!/usr/bin/env python3
"""
飞书消息发送，方式由 config.feishu.mode 决定：
- webhook：群自定义机器人，只需要 Webhook 地址（环境变量 FEISHU_WEBHOOK_URL；
  机器人开了签名校验时再配 FEISHU_WEBHOOK_SECRET）。告警发到 FEISHU_ALERT_WEBHOOK_URL，没配就跳过
- lark-cli：飞书应用机器人，需要安装并初始化 lark-cli，
  告警私信给 feishu.admin_user_ids
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import time
from typing import List

import requests

from common import load_env_var, local_now

# 自定义机器人请求体上限 20 KB，留一点余量
WEBHOOK_BODY_LIMIT = 18 * 1024


# ---------------------------------------------------------------- Webhook

def _sign(secret: str) -> dict:
    timestamp = str(int(time.time()))
    digest = hmac.new(f'{timestamp}\n{secret}'.encode('utf-8'), digestmod=hashlib.sha256).digest()
    return {'timestamp': timestamp, 'sign': base64.b64encode(digest).decode('utf-8')}


def _post_webhook(url: str, secret: str, payload: dict) -> bool:
    body = dict(payload, **_sign(secret)) if secret else payload
    try:
        data = requests.post(url, json=body, timeout=20).json()
    except Exception as e:
        print(f'   ❌ Webhook 请求异常: {e}')
        return False
    # 成功返回 {"code": 0}，旧版接口返回 {"StatusCode": 0}
    if data.get('code', data.get('StatusCode', -1)) != 0:
        print(f'   ❌ Webhook 返回错误: {json.dumps(data, ensure_ascii=False)[:300]}')
        return False
    return True


def _card(title: str, blocks: List[str]) -> dict:
    elements = []
    for i, block in enumerate(blocks):
        if i:
            elements.append({'tag': 'hr'})
        elements.append({'tag': 'markdown', 'content': block})
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue'},
        'elements': elements,
    }


def _card_size(card: dict) -> int:
    return len(json.dumps({'msg_type': 'interactive', 'card': card}, ensure_ascii=False).encode('utf-8'))


def report_to_cards(markdown: str) -> List[dict]:
    """
    日报 Markdown 转消息卡片：首行 # 标题做卡片头，每个 ## 板块一段、板块之间加分割线，
    板块标题转成加粗（卡片 Markdown 不支持 # 标题）。超过请求体上限时按板块拆成多张卡片。
    """
    lines = markdown.strip().splitlines()
    title = '每日资讯'
    if lines and lines[0].startswith('# '):
        title, lines = lines[0][2:].strip(), lines[1:]

    blocks, current = [], []
    for line in lines:
        if line.startswith('## '):
            blocks.append(current)
            current = [f'**{line[3:].strip()}**']
        elif line.strip() != '---':
            current.append(line)
    blocks.append(current)
    blocks = [text for text in ('\n'.join(b).strip() for b in blocks) if text]

    groups, chunk = [], []
    for block in blocks:
        if chunk and _card_size(_card(title, chunk + [block])) > WEBHOOK_BODY_LIMIT:
            groups.append(chunk)
            chunk = []
        chunk.append(block)
    if chunk:
        groups.append(chunk)
    if len(groups) == 1:
        return [_card(title, groups[0])]
    return [_card(f'{title}（{i}/{len(groups)}）', g) for i, g in enumerate(groups, 1)]


# ---------------------------------------------------------------- lark-cli

def _lark_send(target_flag: str, target: str, body_flag: str, body: str, timeout: int = 60) -> bool:
    """target_flag: --chat-id / --user-id；body_flag: --markdown / --text"""
    if not shutil.which('lark-cli'):
        print('   ❌ 未安装 lark-cli')
        return False
    try:
        result = subprocess.run(
            ['lark-cli', 'im', '+messages-send', '--as', 'bot', target_flag, target, body_flag, body],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        print(f'   ❌ lark-cli 调用异常: {e}')
        return False
    if result.returncode != 0:
        print(f'   ❌ lark-cli 发送失败: {(result.stderr or result.stdout).strip()[:500]}')
        return False
    return True


# ---------------------------------------------------------------- 对外接口

def send_report(config: dict, markdown: str) -> bool:
    fs = config.get('feishu', {})
    mode = fs.get('mode', 'webhook')
    if mode == 'webhook':
        url = load_env_var('FEISHU_WEBHOOK_URL')
        if not url:
            print('❌ 未配置 FEISHU_WEBHOOK_URL')
            return False
        secret = load_env_var('FEISHU_WEBHOOK_SECRET')
        cards = report_to_cards(markdown)
        results = []
        for i, card in enumerate(cards):
            if i:
                time.sleep(1)
            results.append(_post_webhook(url, secret, {'msg_type': 'interactive', 'card': card}))
        return all(results)
    if mode == 'lark-cli':
        if not fs.get('chat_id'):
            print('❌ 未配置 feishu.chat_id')
            return False
        return _lark_send('--chat-id', fs['chat_id'], '--markdown', markdown)
    print(f'❌ 未知的 feishu.mode：{mode}（可选 webhook / lark-cli）')
    return False


def send_alert(config: dict, title: str, message: str) -> bool:
    text = (f"⚠️ {config.get('name', '每日资讯')}：{title}\n\n"
            f"时间：{local_now(config):%Y-%m-%d %H:%M:%S}\n{message}")
    fs = config.get('feishu', {})
    if fs.get('mode', 'webhook') == 'lark-cli':
        admins = fs.get('admin_user_ids') or []
        if not admins:
            print('   ℹ️ 未配置 feishu.admin_user_ids，跳过告警')
            return False
        sent = any([_lark_send('--user-id', uid, '--text', text, timeout=30) for uid in admins])
    else:
        url = load_env_var('FEISHU_ALERT_WEBHOOK_URL')
        if not url:
            print('   ℹ️ 未配置 FEISHU_ALERT_WEBHOOK_URL，跳过告警')
            return False
        sent = _post_webhook(url, load_env_var('FEISHU_ALERT_WEBHOOK_SECRET'),
                             {'msg_type': 'text', 'content': {'text': text}})
    if sent:
        print('   📢 已发送告警')
    return sent
