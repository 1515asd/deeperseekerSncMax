#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 桥内登录一次性脚本（本地运行版）

用途：在【你自己的电脑/手机热点】（住宅 IP，WAF 必放行）上，
用官方安卓 App 的登录协议登录一次 DeepSeek，拿到 token。
把输出的 token 粘贴进桥面板的「贴入令牌」表单即可。

为什么这样做：2026-11 起 DeepSeek 对数据中心 IP 的登录端点
（POST /api/v0/users/login）上 AWS WAF 质询，服务器侧无法登录。
但会话的「出生环境」是关键——在住宅 IP 用安卓协议出生的会话，
之后在桥（安卓形态）里使用是被社区实践验证可行的
（DSH 系项目的 token 同样出生在家庭浏览器、跑在服务器上）。

用法：
    pip install requests   # 只需要这一个库
    python3 login_once.py

    按提示输入：
      1. 账号（邮箱或手机号）
      2. 密码
      3. 设备指纹（可选，强烈建议填：Chrome 打开 chat.deepseek.com
         无需登录，F12 控制台执行 SMSdk.getDeviceId()，把输出粘进来）

安全说明：
  - 脚本只在本机运行，密码只发给 DeepSeek 官方登录接口，不经过
    任何第三方（含本脚本的作者）。
  - 输出的 token 等同账号凭据，只粘贴到你自己的桥面板。
"""

import json
import sys

try:
    import requests
except ImportError:
    print("请先安装 requests：pip install requests")
    sys.exit(1)

LOGIN_URL = "https://chat.deepseek.com/api/v0/users/login"


def main():
    print("=" * 56)
    print("DeepSeek 桥内登录一次性脚本（请在住宅网络下运行）")
    print("=" * 56)
    account = input("账号（邮箱或手机号）：").strip()
    password = input("密码：").strip()
    device_id = input("设备指纹（可选，直接回车跳过）：").strip()

    if not account or not password:
        print("账号和密码都必须填写。")
        sys.exit(1)

    if "@" in account:
        email, mobile = account, None
    else:
        email, mobile = None, account

    if not device_id:
        print("\n提示：未填设备指纹。实测随机指纹会被风控拒绝"
              "（RISK_DEVICE_DETECTED）。如果登录失败提示设备风控，"
              "请按脚本顶部说明获取真实指纹后重试。\n")

    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/sign_in",
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7)",
        "x-client-platform": "android",
        "x-client-version": "2.4.5",
        "x-client-locale": "en_US",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-timezone-offset": "28800",
        "x-device-model": "",
    }
    body = {
        "email": email,
        "mobile": mobile,
        "password": password,
        "area_code": "+86",
        "device_id": device_id or "00000000-0000-0000-0000-000000000000",
        "os": "android",
    }

    try:
        r = requests.post(LOGIN_URL, headers=headers, json=body, timeout=20)
    except Exception as exc:
        print(f"网络请求失败：{exc}")
        sys.exit(1)

    if r.status_code != 200 or "application/json" not in r.headers.get(
            "content-type", ""):
        print(f"登录端点被拦截（HTTP {r.status_code}，"
              f"{r.headers.get('content-type', '')}）。")
        print("你的出口 IP 正被 WAF 质询——请换到家庭宽带/手机流量"
              "（不要用公司/校园网或 VPN）再试。")
        sys.exit(1)

    d = r.json()
    if d.get("code") != 0:
        print(f"上游异常：{str(d)[:200]}")
        sys.exit(1)

    data = d.get("data") or {}
    biz = data.get("biz_code")
    if biz not in (None, 0):
        reason = {
            2: "密码或账号错误（biz_code=2）",
            10: "账号已被封禁（biz_code=10）",
            11: "设备风控拒绝（biz_code=11）——请填真实设备指纹后重试，"
                "获取方法见脚本顶部说明",
        }.get(biz, f"业务拒绝 biz_code={biz}：{data.get('biz_msg', '')}")
        print(f"登录失败：{reason}")
        sys.exit(1)

    token = ((data.get("biz_data") or {}).get("user") or {}).get("token")
    if not token:
        print(f"应答异常（未找到 token）：{str(d)[:200]}")
        sys.exit(1)

    user = (data.get("biz_data") or {}).get("user") or {}
    chat = user.get("chat") or {}
    print("\n" + "=" * 56)
    print("登录成功！")
    if chat.get("is_muted"):
        import datetime
        until = chat.get("mute_until")
        if until:
            t = datetime.datetime.fromtimestamp(until / 1000)
            print(f"⚠️ 该账号当前被禁言，解除时间：{t:%Y-%m-%d %H:%M}")
    print("\n把下面这一整串粘贴到桥面板「方式二 · 手动粘贴令牌」：\n")
    print(token)
    print("\n（也可以直接把账号+密码+本次用的设备指纹填进桥面板的"
          "「方式一 · 桥内登录」，等出口 IP 解封后自动重登就能用了）")
    print("=" * 56)


if __name__ == "__main__":
    main()
