#!/bin/bash
# AutoProxy · ffmpeg 自检与自动兜底安装
# 由 install_callback / config_callback 调用（这些阶段通常具备 root 权限）。
# 行为：
#   1) 已存在 ffmpeg（内置 app/bin、/usr/bin 或 PATH）→ 直接成功，什么都不做。
#   2) 缺失且用户同意自动安装（向导开关 wizard_auto_install_ffmpeg，默认开启）→ 尝试 apt 补齐。
#   3) 失败或未同意 → 静默降级，由守护进程/Web 界面继续提示，绝不阻断安装。

# 已存在则成功
command -v ffmpeg >/dev/null 2>&1 && exit 0
[ -x "$TRIM_APPDEST/bin/ffmpeg" ] 2>/dev/null && exit 0
[ -x /usr/bin/ffmpeg ] 2>/dev/null && exit 0
[ -x /usr/local/bin/ffmpeg ] 2>/dev/null && exit 0

# 用户是否同意自动安装（宽松匹配，未设置时默认同意）
AUTO="${wizard_auto_install_ffmpeg:-true}"
case "$AUTO" in
  true|1|on|yes|TRUE|True|On|ON|Yes|YES) ;;
  *) exit 0 ;;
esac

# 仅在具备 root 权限且存在 apt 时尝试自动安装（失败静默降级）
if [ "$(id -u)" = "0" ] && command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 || true
  apt-get install -y -qq ffmpeg >/dev/null 2>&1 || true
fi

exit 0
