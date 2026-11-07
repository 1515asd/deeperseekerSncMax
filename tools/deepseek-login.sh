#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# DeepSeek 一次性登录（安卓 Termux 版 / HTTP Shortcuts cURL 导入版）
#
# 用法一（Termux 直接跑）：
#   pkg install curl -y
#   bash deepseek-login.sh
#
# 用法二（HTTP Shortcuts App 导入）：
#   安装开源应用 HTTP Shortcuts（F-Droid 或 GitHub Releases）
#   → 右上角 ⋮ → 导入 → 从 cURL 命令导入
#   → 把下面「curl -s -X POST ...」整段（到 --data 行结尾）复制进去
#   → 导入后打开快捷指令，把 body 里的
#       "email": "you@example.com" 和 "password": "YOUR_PASSWORD"
#     改成你的账号密码（建议在 App 里建变量，别写死在 body 里）
#   → 桌面放小组件，点一下即登录，令牌显示在结果里
#
# 安全：密码只发给 DeepSeek 官方登录接口，不经过任何第三方。
# ==============================================================================

read -p "账号（邮箱或手机号）：" ACCOUNT
read -s -p "密码：" PASSWORD; echo
read -p "设备指纹（可选，直接回车跳过）：" DEVICE_ID
[ -z "$DEVICE_ID" ] && DEVICE_ID="00000000-0000-0000-0000-000000000000"

if [[ "$ACCOUNT" == *@* ]]; then
  EMAIL="$ACCOUNT"; MOBILE=null
else
  EMAIL=null; MOBILE="\"$ACCOUNT\""
fi

curl -s -X POST "https://chat.deepseek.com/api/v0/users/login" \
  -H "content-type: application/json" \
  -H "origin: https://chat.deepseek.com" \
  -H "referer: https://chat.deepseek.com/sign_in" \
  -H "user-agent: Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7)" \
  -H "x-client-platform: android" \
  -H "x-client-version: 2.4.5" \
  -H "x-client-locale: en_US" \
  -H "x-client-bundle-id: com.deepseek.chat" \
  -H "x-client-timezone-offset: 28800" \
  -d "{\"email\":${EMAIL},\"mobile\":${MOBILE},\"password\":\"${PASSWORD}\",\"area_code\":\"+86\",\"device_id\":\"${DEVICE_ID}\",\"os\":\"android\"}"

echo
echo "↑ 应答里 data.biz_data.user.token 的值就是令牌，复制它贴进桥面板。"
echo "  （biz_code=2 密码错；10 封号；11 设备风控——需填真实指纹，方法见仓库说明）"
