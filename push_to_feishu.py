#!/usr/bin/env python3
"""
把今天的日报推送到飞书群（推送方式见 feishu.py），失败时发告警并以非 0 退出。
  python3 push_to_feishu.py --dry-run   # 只打印转换后的消息卡片，不发送
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from common import BASE_DIR, load_config, report_path, today_str
from feishu import report_to_cards, send_alert, send_report


def main() -> int:
    parser = argparse.ArgumentParser(description='推送日报到飞书')
    parser.add_argument('--dry-run', action='store_true', help='只打印消息卡片 JSON，不发送')
    args = parser.parse_args()

    config = load_config()
    path = report_path(today_str(config))
    rel = os.path.relpath(path, BASE_DIR)
    if not os.path.exists(path):
        print(f'ℹ️ 今天没有日报（{rel}），跳过推送')
        return 0
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    mode = config.get('feishu', {}).get('mode', 'webhook')
    print(f'📤 推送 {rel}（{len(content)} 字符，方式：{mode}）')
    if args.dry_run:
        for card in report_to_cards(content):
            body = json.dumps({'msg_type': 'interactive', 'card': card}, ensure_ascii=False, indent=2)
            print(f'--- 卡片（{len(body.encode("utf-8"))} 字节）---\n{body}')
        return 0

    if send_report(config, content):
        print('✅ 推送成功')
        return 0
    send_alert(config, '日报推送失败', f'报告文件：{rel}\n请检查飞书推送配置（Webhook / lark-cli）和网络。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
