# 达芬奇自动代理（AutoProxy）项目交接文档

> 本文档汇总项目工程文件、开发历程关键决策、踩坑记录与飞牛官方开发文档 URL，供后续会话继续开发/维护使用。
> 最后更新：2026-10-12

---

## 一、项目概述

**项目名**：AutoProxy（显示名「达芬奇自动代理」，版本 1.2.0）

**定位**：跑在飞牛 fnOS（原生应用，非 Docker）上的后台转码服务——监控素材目录，检测到新增视频后自动调用 ffmpeg 转码为低清代理，供 **DaVinci Resolve（达芬奇）** 剪辑使用。

**核心能力**：
- 纯 Python 3 标准库实现（无第三方依赖），`platform=all` 兼容 x86/ARM。
- 多目录监控 + 值守防闪退（watchdog 3 秒自动重启）。
- 代理编码：DNxHR LB / ProRes Proxy / H.264 / H.265；可选 VAAPI/NVENC 硬件加速。
- 保留文件名 + 时间码，代理输出到各目录自身的 `Proxy` 子文件夹（达芬奇可自动重链）。
- 内置 Web 管理界面（任务队列/日志/立即扫描/重试/清理）。
- 点击按钮可唤起飞牛文件管理器选择监控目录（`@trimjs/web-app` SDK）。
- 接入飞牛统一网关鉴权，不监听公网端口；路径穿越防护。

**服务器**：
- NAS 内网 IP：`192.168.0.102`（飞牛 Web 界面端口 `5666`）
- SSH：`admin@192.168.0.102`（密码见本地私密记录，**请勿写入代码仓库**）

---

## 二、工程文件结构

项目根目录：`C:\Users\Admin\WorkBuddy\2026-09-17-22-34-41\AutoProxy\`

```
AutoProxy/
├── manifest                  # 应用元信息（见下方关键字段）
├── ICON.PNG / ICON_256.PNG   # 应用图标（达芬奇风格：深色圆 + RGB 色环 + 向下箭头）
├── config/
│   ├── privilege             # 运行身份（run-as=package，专用用户 autoproxy，加入 video/render 组）
│   └── resource              # api-scope 声明：["trim.file.userAccess"]
├── wizard/
│   ├── install               # 安装向导：codec / resolution / hwaccel / auto_install_ffmpeg
│   └── config                # 设置表单（同上，改配置走此表单）
├── cmd/                      # 生命周期脚本（由飞牛以 root 调用）
│   ├── main                  # start/stop/status + watchdog 值守循环
│   ├── install_init / install_callback
│   ├── upgrade_init / upgrade_callback
│   ├── uninstall_init / uninstall_callback
│   ├── config_init / config_callback   # config_callback 末尾调用 fix_gateway.sh
│   └── fix_gateway.sh        # 网关路由自愈脚本（幂等，见第四节）
├── app/
│   ├── src/autoproxy.py      # 转码守护进程（核心，约 850 行）
│   ├── web/index.html        # Web 管理界面（含 toast、BASE 前缀、文件选择器）
│   ├── web/trim-sdk.js       # 飞牛前端 JS SDK（@trimjs/web-app，ESM，已 bundle）
│   ├── bin/write_config.py   # 向导值 → config.json
│   ├── bin/ensure_ffmpeg.sh  # ffmpeg 自检/自动补齐
│   └── ui/
│       ├── config            # 桌面入口（gatewaySocket/gatewayPrefix）
│       └── images/           # icon_64.png / icon_256.png
├── tools/
│   ├── fnpack.exe            # 打包工具（Windows 版）
│   └── gen_icons.py          # 图标生成脚本（可重跑）
├── AutoProxy.fpk             # 打包产物（1.2.0）
└── README.md                 # 项目说明
```

### manifest 关键字段
```ini
appname="AutoProxy"          # 内部标识，切勿改动（改则破坏升级链）
version="1.2.0"
display_name="达芬奇自动代理"
source="thirdparty"
platform="all"
micro_app=true               # 启用前端 JS SDK 前提
checkport=false              # 统一网关模式不监听端口，不检查
changelog="..."              # 更新说明
desktop_applaunchname="AutoProxy.main"
ctl_stop=true
disable_authorization_path=false   # 保留授权目录入口
```

---

## 三、核心技术架构

### 1. 统一网关接入（重点，也是踩坑最多的地方）

飞牛统一网关让应用在系统访问域名下获得稳定 URL，网关先校验登录态再转发到应用本地 Unix Socket。

```
浏览器 → 飞牛网关(5666, 校验登录态) → /app/autoproxy → Unix Socket → 应用
```

**接入要素**：
- `app/ui/config` 声明：`type=iframe` + `gatewaySocket="autoproxy.sock"`（相对文件名）+ `gatewayPrefix="/app/autoproxy"` + `url="/app/autoproxy"`。
- `manifest`：`micro_app=true`、`checkport=false`、**不设 service_port**。
- `cmd/main` 用 `--socket "$TRIM_APPDEST/autoproxy.sock"` 启动守护进程。
- 守护进程监听 Unix Socket（`ThreadingUnixHTTPServer` = `ThreadingMixIn + UnixStreamServer`），**不监听任何 TCP 端口**。
- 鉴权由飞牛网关的登录态完成（未登录返回 `invalid token`），应用自身不鉴权；网关还通过 Header 转发用户信息（`X-Trim-Userid`/`X-Trim-Isadmin`/`X-Trim-Username`）。

**两个必须处理的关键点**：
1. **网关转发保留前缀**：应用收到的是 `/app/autoproxy/...`，须用 `strip_gateway_prefix()` 剥前缀后再路由（后端路由仍是 `/api/...`、`/`）。
2. **前端路径基于动态 BASE**：`index.html` 用 `location.pathname` 推导 `BASE`，所有 API/静态资源请求拼 `BASE + ...`，避免绝对路径 `/api/...` 绕过前缀。

### 2. 鉴权模型
- 生产（Unix Socket 模式）：飞牛网关鉴权，应用不监听端口。
- 兜底（TCP 回环模式）：`--auth-token` 参数 + `_check_auth()`（`Authorization: Bearer` 或 `?token=`），仅监听 `127.0.0.1`。

### 3. 路径安全
- `validate_watch_path()`：添加监控目录时校验绝对路径、拒绝 `.`/`..`、根目录、系统目录（/etc /usr /proc 等）。
- `_serve_static()`：静态文件服务用 `normpath + startswith(base)` 防目录穿越。

### 4. 代理存储逻辑
- `proxy_path_for(src, codec)`：代理放在**源文件所在目录**的 `Proxy` 子文件夹（紧挨源文件，不镜像子目录结构）。
- `os.walk` 遍历时 `dirs[:]` 过滤掉 `Proxy` 目录，避免把代理当源文件。

### 5. 值守（防闪退）
- `cmd/main` 的 `start` 起一个 while 循环 watch 主进程，闪退 3 秒后自动重启。
- `stop` 和 `uninstall_init` 必须 `pkill -f "[c]md/main start"`（用 `[c]` 字符类避免 pkill 自匹配）杀 watchdog 循环，否则残留。

---

## 四、关键决策与踩坑记录（对话重点）

按时间/主题汇总整个开发历程的关键点：

### A. 目录选择：从手填路径 → 飞牛文件管理器
- 飞牛向导表单只有 7 种字段（text/password/radio/checkbox/select/switch/tips），**没有目录选择器**。
- 改用飞牛「前端 JS SDK」`@trimjs/web-app` 的 `pickUserFile({directory:true})`，点击按钮唤起飞牛文件管理器选目录并自动授权。
- 前置：`manifest` 加 `micro_app=true`；`config/resource` 声明 `api-scope:["trim.file.userAccess"]`。

### B. 统一网关接入的完整排障链（最核心的教训）
1. **入口 url 必须带尾斜杠**：飞牛网关 location 是带尾斜杠匹配的，`url="/app/autoproxy"` 会 404，`url="/app/autoproxy/"` 才正常（最终用 iframe 后 url 用 `/app/autoproxy`）。
2. **入口 type 必须 iframe**：`type=url` 时 appcenter 不解析 gateway 字段（数据库里空）。
3. **手动安装（GUI/CLI）不解析 gateway 字段**：飞牛 appcenter 只对「应用商店/源」安装（`manual_install=f`，如 frpc）解析 `gatewaySocket/gatewayPrefix`；手动安装（`manual_install=t`，无论 GUI 上传还是 appcenter-cli）都不解析，导致 `app_service` 表的 gateway 字段为空 → 网关 404 "not found"。**这是飞牛 appcenter 的实现缺失，不是我们配置错**（官方文档确认配置写法正确）。
4. **网关转发保留前缀**：应用端须剥前缀（见第三节）。
5. **重启网关会失效所有登录态**：`systemctl restart trim_open_gateway` 后用户需重新登录飞牛；`reload` 不支持（"Job type reload is not applicable"）。

### C. 路由自愈方案（针对 B3 的兜底）
- `cmd/fix_gateway.sh`（幂等）：检测 `appcenter.app_service` 的 `gateway_socket`/`gateway_prefix` 是否等于预期值，不一致才 `update` + `systemctl restart trim_open_gateway`，一致则直接返回（不重启网关）。
- 挂载点：`install_callback`、`config_callback` 末尾 `( sleep 8; sh fix_gateway.sh ) &` 延迟调用（避开 appcenter 随后重建 app_service 的时序）。
- 代价：安装/改配置后约 8 秒自动补路由并重启网关一次，需重新登录飞牛，但路由不再丢。

### D. 手动补路由的命令（应急）
```bash
sudo -u postgres psql -d appcenter -c \
 "update app_service set gateway_socket='/vol1/@appcenter/AutoProxy/autoproxy.sock', gateway_prefix='/app/autoproxy' \
  where app_id=(select id from app where app_name='AutoProxy')"
systemctl restart trim_open_gateway
```
> 注意：`app_id` 每次重装都会变（39→40→41...），必须用子查询动态取，硬编码会 UPDATE 0 行。

### E. 其他踩坑
- **watchdog 残留进程**：旧版 stop/uninstall 只杀主进程不杀 watchdog，导致旧 watchdog 用旧参数反复重启、抢占端口。修复：加 `pkill -f "[c]md/main start"`。
- **pkill 自匹配**：`pkill -f "xxx"` 会匹配到含该字符串的 sudo 命令自身而自杀，用 `[x]xx` 字符类规避。
- **sudo 只作用于第一个命令**：`sudo pkill A; pkill B` 里只有 A 是 root，多命令要用 `sudo bash -c '...'` 包裹。
- **fnpack 打包会重新格式化 manifest**：输出 `key = value`（空格对齐），验证字符串用 `'micro_app = true'` 而非 `'micro_app=true'`。
- **同一文件多处 Edit 会互相覆盖**：改同一文件多处时用整文件 Write 重写 + 立即校验，不要一条消息发多个 Edit。
- **写库用动态 id**（见 D）。
- **write_config.py 需兼容旧 `watch_dir` 单字段**：否则改配置会把监控目录清空。

### F. 品牌与交付
- 图标：达芬奇风格原创设计（深色圆底 + RGB 三色环 + 白色向下箭头），`tools/gen_icons.py` 生成。**未直接复制 Blackmagic 官方图标**（版权/商标）。
- 名称：`display_name="达芬奇自动代理"`（`appname` 保持 `AutoProxy` 不变）。
- 简介含联系方式：微信 15366902445（desc 与 maintainer 字段）。
- 版本号/changelog 同步：1.2.0。

---

## 五、飞牛官方开发文档 URL

| 主题 | URL |
|------|-----|
| 统一网关（gateway 注册） | https://developer.fnnas.com/docs/core-concepts/gateway-registration/ |
| 应用入口（app/ui/config） | https://developer.fnnas.com/docs/core-concepts/app-entry/ |
| Manifest 字段 | https://developer.fnnas.com/docs/core-concepts/manifest/ |
| index.cgi（轻量入口） | https://developer.fnnas.com/docs/core-concepts/index-cgi/ |
| API 调用方式（JS SDK + 后端 API） | https://developer.fnnas.com/api/calling/ |
| 平台配置 API | https://developer.fnnas.com/api/platform-config/ |
| 前端 JS SDK npm 包 | https://www.npmjs.com/package/@trimjs/web-app |
| 打包工具 fnpack | https://developer.fnnas.com/docs/cli/fnpack/ |
| 文档根（导航） | https://developer.fnnas.com/docs/ |

> 补充参考（社区/第三方，非官方）：leelaa 博客「如何在飞牛中使用统一网关」`https://www.leelaa.cn/p/adeofa.html`；fn-WireGuard-web `https://github.com/sdwsljy/fn-WireGuard-web`（Python 标准库 REST + Unix Socket 接入网关的参考实现）。

---

## 六、部署与运行信息

**飞牛侧路径**：
- 程序目录（`TRIM_APPDEST`）：`/vol1/@appcenter/AutoProxy`
- 配置目录（`TRIM_PKGETC`）：`/vol1/@appconf/AutoProxy`（config.json 在此）
- 数据目录（`TRIM_PKGVAR`）：`/vol1/@appdata/AutoProxy`（jobs.db、autoproxy.log）
- 生命周期脚本：`/var/apps/AutoProxy/cmd/`（main、install_callback、config_callback、fix_gateway.sh 等）
- 网关 socket：`/vol1/@appcenter/AutoProxy/autoproxy.sock`
- 运行用户：`autoproxy`（专用用户，run-as=package）

**config.json 当前值**（示例）：
```json
{
  "watch_dirs": ["/vol1/1000/胜利酒店相关文件/素材/20250323亲子楼层素材/视频"],
  "codec": "h264", "resolution": "half", "port": 9080,
  "hwaccel": "auto", "scan_interval": 5, "stable_wait": 10, "workers": 1
}
```

**访问方式**：飞牛桌面打开应用，或直接访问 `http://192.168.0.102:5666/app/autoproxy/`。

**打包命令**（Windows）：在项目根目录运行 `tools\fnpack.exe build`，产出 `AutoProxy.fpk`。

---

## 七、当前状态与遗留问题

**当前状态**：应用 1.2.0 已部署运行，统一网关接入完整（鉴权、不监听端口、路径防护、文件管理器选目录、代理就近存储）均已实现并验证。

**遗留/待优化**：
1. **手动安装不解析 gateway 是飞牛 appcenter 的行为缺失**，当前靠 `fix_gateway.sh` 自愈兜底。彻底根治需把应用上架到某个飞牛第三方源（使 `manual_install=f`），未实施。
2. **改配置/重装会触发网关重启**（网关不支持热重载），导致用户需重新登录飞牛一次。这是飞牛网关限制，暂无法消除。
3. **改配置会重启应用进程**（config_callback 里 pkill + start），有 3~5 秒空窗；如需平滑可改为热重载配置（未做）。
4. 若监控目录未来出现子文件夹结构，旧的「集中根 Proxy」代理文件不会被新「就近 Proxy」逻辑识别，可能重复转码，届时需手动清理旧根 Proxy。

---

## 八、其他会话接手建议

- 先读 `app/src/autoproxy.py`（核心逻辑：Watcher/Worker/Handler/App + `strip_gateway_prefix`/`validate_watch_path`/`proxy_path_for`）。
- 改飞牛集成相关（manifest/入口/生命周期）务必参考第四节 A~D 的坑。
- 本地开发环境是 Windows（无 AF_UNIX），Unix Socket 相关逻辑只能在 fnOS 上实测；本地可用 `--auth-token` 走 TCP 回环模式验证。
- 部署/SSH 凭据通过环境变量或本地私密文件提供，**不要硬编码进仓库**。
