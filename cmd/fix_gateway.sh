#!/bin/bash
# 统一网关路由自愈脚本（幂等 + 多次复核）。
#
# 背景：飞牛「手动安装」（应用中心上传 fpk 或 appcenter-cli）的第三方应用，安装/改配置后
# appcenter 会重建 app_service 记录，但 gateway_socket/gateway_prefix 会被清空或写成
# 不带 TRIM_APPDEST 前缀的相对路径（如 /autoproxy.sock），统一网关访问 404 Not Found。
#
# 注意时序：appcenter 可能在 install_callback 之后仍继续重建记录，单次修复可能被覆盖，
# 因此本脚本在约 2 分钟内多次复核（每 12 秒一次，共 10 次），发现不一致才 update 并
# 重启网关；字段始终正确时静默退出（不重启网关，无副作用）。
set +e

APP_NAME="${TRIM_APPNAME:-AutoProxy}"
SOCK="${TRIM_APPDEST:-/vol1/@appcenter/AutoProxy}/autoproxy.sock"
PREFIX="/app/autoproxy"

fix_once() {
  APP_ID=$(sudo -u postgres psql -d appcenter -Atc "select id from app where app_name='${APP_NAME}'" 2>/dev/null | tr -d '[:space:]')
  [ -z "$APP_ID" ] && return 1
  CUR_SOCK=$(sudo -u postgres psql -d appcenter -Atc "select gateway_socket from app_service where app_id=${APP_ID}" 2>/dev/null | tr -d '[:space:]')
  CUR_PRE=$(sudo -u postgres psql -d appcenter -Atc "select gateway_prefix from app_service where app_id=${APP_ID}" 2>/dev/null | tr -d '[:space:]')
  if [ "$CUR_SOCK" = "$SOCK" ] && [ "$CUR_PRE" = "$PREFIX" ]; then
    return 0
  fi
  sudo -u postgres psql -d appcenter -c "update app_service set gateway_socket='${SOCK}', gateway_prefix='${PREFIX}' where app_id=${APP_ID}" >/dev/null 2>&1
  systemctl restart trim_open_gateway >/dev/null 2>&1
  return 0
}

# 立即修一次，随后每 12 秒复核一次，共 10 轮（覆盖 appcenter 延迟重建的窗口）
i=0
while [ $i -lt 10 ]; do
  fix_once
  i=$((i + 1))
  [ $i -lt 10 ] && sleep 12
done
exit 0
