#!/usr/bin/env python3
"""
查飞书应用机器人的配置信息：机器人所在的群（chat_id → feishu.chat_ids）和群成员（open_id → feishu.admin_user_ids）。
先把机器人拉进群、在 .env 填好 LARK_APP_ID / LARK_APP_SECRET：
  python3 feishu_ids.py          # 列出所有群，每个群显示前 30 位成员
  python3 feishu_ids.py 张三     # 只显示名字里含"张三"的成员
"""

from __future__ import annotations

import sys

from feishu import FeishuError, api, api_items

MEMBER_PREVIEW = 30


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        bot = api('GET', '/bot/v3/info').get('bot') or {}
        print(f"🤖 机器人：{bot.get('app_name', '?')}（open_id {bot.get('open_id', '?')}）")
        chats = api_items('/im/v1/chats')
        if not chats:
            print('ℹ️ 机器人还不在任何群里：在群设置 → 群机器人 → 添加机器人，搜索应用名加入')
            return 0
        print(f'📋 机器人所在的群（{len(chats)} 个）：')
        for chat in chats:
            print(f"\n  · {chat.get('name') or '(未命名群)'}：{chat['chat_id']}")
            members = api_items(f"/im/v1/chats/{chat['chat_id']}/members", {'member_id_type': 'open_id'})
            shown = [m for m in members if keyword in (m.get('name') or '')] if keyword else members[:MEMBER_PREVIEW]
            for m in shown:
                print(f"      {m.get('name', '?')}：{m['member_id']}")
            if not keyword and len(members) > MEMBER_PREVIEW:
                print(f'      …共 {len(members)} 人，可以加名字关键字筛选')
    except FeishuError as e:
        print(f'❌ {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
