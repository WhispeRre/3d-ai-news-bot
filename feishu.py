#!/usr/bin/env python3
"""
飞书消息发送，方式由 config.feishu.mode 决定：
- app：飞书应用机器人。凭 App ID / App Secret（环境变量 LARK_APP_ID / LARK_APP_SECRET）调开放接口，
  日报发到 feishu.chat_ids 里的每个群（机器人需已入群）并私信 feishu.user_ids 里的每个人，
  告警私信给 feishu.admin_user_ids（私信对象都需在应用可用范围内）
- webhook：群自定义机器人，只需要 Webhook 地址（FEISHU_WEBHOOK_URL；开了签名校验再配 FEISHU_WEBHOOK_SECRET）
两种方式发的是同一种消息卡片。告警发不出私信（或没配管理员）时退回 FEISHU_ALERT_WEBHOOK_URL，
这样应用凭证失效导致推送失败时，告警仍能送达。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import List, Optional

import requests

from common import load_env_var, local_now

API_BASE = 'https://open.feishu.cn/open-apis'

# 卡片请求体上限：自定义机器人 20 KB、开放接口 30 KB，统一按小的留余量
CARD_BODY_LIMIT = 18 * 1024

_ERROR_HINTS = {
    230002: '机器人不在这个群里，先在群设置 → 群机器人里把应用加进来',
    230013: '对方不在应用的可用范围内，去开发者后台调整可用范围并发布新版本',
    232011: '机器人不在这个群里',
    232025: '应用没开启机器人能力，或开启后还没发布新版本',
    99991672: '应用缺少接口权限，或开通权限后还没发布新版本',
}


class FeishuError(RuntimeError):
    pass


# ---------------------------------------------------------------- 消息卡片

def _card(title: str, blocks: List[str], color: str = 'blue') -> dict:
    elements = []
    for i, block in enumerate(blocks):
        if i:
            elements.append({'tag': 'hr'})
        elements.append({'tag': 'markdown', 'content': block})
    return {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': color},
        'elements': elements,
    }


def _card_size(card: dict) -> int:
    return len(json.dumps({'msg_type': 'interactive', 'card': card}, ensure_ascii=False).encode('utf-8'))


def report_to_cards(markdown: str, color: str = 'blue') -> List[dict]:
    """
    日报 Markdown 转消息卡片：首行 # 标题做卡片头，每个 ## 板块一段、板块之间加分割线，
    板块标题转成加粗（卡片 Markdown 不支持 # 标题）。超过请求体上限时按板块拆成多张卡片。
    color 是卡片头的颜色（飞书卡片模板色：blue、indigo、purple、green 等）
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
        if chunk and _card_size(_card(title, chunk + [block], color)) > CARD_BODY_LIMIT:
            groups.append(chunk)
            chunk = []
        chunk.append(block)
    if chunk:
        groups.append(chunk)
    if len(groups) == 1:
        return [_card(title, groups[0], color)]
    return [_card(f'{title}（{i}/{len(groups)}）', g, color) for i, g in enumerate(groups, 1)]


# ---------------------------------------------------------------- 自定义机器人 Webhook

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


# ---------------------------------------------------------------- 应用机器人（开放接口）

_token_cache: dict = {}


def tenant_token() -> str:
    if _token_cache.get('expires', 0) > time.time():
        return _token_cache['token']
    app_id, app_secret = load_env_var('LARK_APP_ID'), load_env_var('LARK_APP_SECRET')
    if not app_id or not app_secret:
        raise FeishuError('未配置 LARK_APP_ID / LARK_APP_SECRET')
    data = requests.post(f'{API_BASE}/auth/v3/tenant_access_token/internal',
                         json={'app_id': app_id, 'app_secret': app_secret}, timeout=20).json()
    if data.get('code') != 0:
        raise FeishuError(f"获取 tenant_access_token 失败：{data.get('msg')}（code {data.get('code')}）")
    _token_cache.update(token=data['tenant_access_token'], expires=time.time() + data.get('expire', 7200) - 300)
    return _token_cache['token']


def api(method: str, path: str, *, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
    """调开放接口，返回完整响应 JSON；code 非 0 时抛 FeishuError（附常见错误的排查提示）"""
    resp = requests.request(method, f'{API_BASE}{path}', params=params, json=body, timeout=20,
                            headers={'Authorization': f'Bearer {tenant_token()}'})
    try:
        data = resp.json()
    except ValueError:
        raise FeishuError(f'HTTP {resp.status_code}：{resp.text[:200]}')
    code = data.get('code', -1)
    if code != 0:
        hint = _ERROR_HINTS.get(code)
        raise FeishuError(f"{data.get('msg', '')}（code {code}）" + (f'。{hint}' if hint else ''))
    return data


def api_items(path: str, params: Optional[dict] = None) -> List[dict]:
    """分页接口的全部 items"""
    items, page_token = [], ''
    while True:
        query = dict(params or {}, page_size=100, **({'page_token': page_token} if page_token else {}))
        data = api('GET', path, params=query).get('data') or {}
        items.extend(data.get('items') or [])
        page_token = data.get('page_token', '')
        if not data.get('has_more') or not page_token:
            return items


def _app_send(receive_id_type: str, receive_id: str, msg_type: str, content: dict) -> None:
    api('POST', '/im/v1/messages', params={'receive_id_type': receive_id_type},
        body={'receive_id': receive_id, 'msg_type': msg_type, 'content': json.dumps(content, ensure_ascii=False)})


# ---------------------------------------------------------------- 对外接口

def send_report(config: dict, markdown: str) -> bool:
    fs = config.get('feishu', {})
    mode = fs.get('mode', 'webhook')
    cards = report_to_cards(markdown, fs.get('card_color', 'blue'))
    if mode == 'app':
        targets = ([('chat_id', c) for c in fs.get('chat_ids') or []]
                   + [('open_id', u) for u in fs.get('user_ids') or []])
        if not targets:
            print('❌ 未配置 feishu.chat_ids 或 feishu.user_ids')
            return False
        ok = True
        for id_type, target in targets:
            try:
                for i, card in enumerate(cards):
                    if i:
                        time.sleep(1)
                    _app_send(id_type, target, 'interactive', card)
            except Exception as e:
                print(f"   ❌ 推送到{'群' if id_type == 'chat_id' else '用户'} {target} 失败：{e}")
                ok = False
        return ok
    if mode == 'webhook':
        url = load_env_var('FEISHU_WEBHOOK_URL')
        if not url:
            print('❌ 未配置 FEISHU_WEBHOOK_URL')
            return False
        secret = load_env_var('FEISHU_WEBHOOK_SECRET')
        results = []
        for i, card in enumerate(cards):
            if i:
                time.sleep(1)
            results.append(_post_webhook(url, secret, {'msg_type': 'interactive', 'card': card}))
        return all(results)
    print(f'❌ 未知的 feishu.mode：{mode}（可选 app / webhook）')
    return False


def send_alert(config: dict, title: str, message: str) -> bool:
    text = (f"⚠️ {config.get('name', '每日资讯')}：{title}\n\n"
            f"时间：{local_now(config):%Y-%m-%d %H:%M:%S}\n{message}")
    fs = config.get('feishu', {})
    if fs.get('mode') == 'app' and fs.get('admin_user_ids'):
        sent = False
        for open_id in fs['admin_user_ids']:
            try:
                _app_send('open_id', open_id, 'text', {'text': text})
                sent = True
            except Exception as e:
                print(f'   ❌ 告警私信 {open_id} 失败：{e}')
        if sent:
            print('   📢 已私信告警')
            return True
    url = load_env_var('FEISHU_ALERT_WEBHOOK_URL')
    if not url:
        print('   ℹ️ 没有可用的告警渠道（私信未配置或失败，也没配 FEISHU_ALERT_WEBHOOK_URL），跳过告警')
        return False
    sent = _post_webhook(url, load_env_var('FEISHU_ALERT_WEBHOOK_SECRET'),
                         {'msg_type': 'text', 'content': {'text': text}})
    if sent:
        print('   📢 已发送告警')
    return sent
