#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoProxy —— fnOS 自动代理转码守护进程
========================================
监控多个目录，检测新增视频文件，自动调用 ffmpeg 转码为低清代理，
供 DaVinci Resolve 剪辑使用。

设计要点：
  * 仅依赖 Python 3 标准库，无第三方包。
  * 转码时保留「文件名 + 时间码」，便于达芬奇自动重链。
  * 输出到每个监控目录下的 Proxy 子文件夹，并镜像相对目录结构。
  * 支持运行中通过 Web 界面/API 新增、删除监控目录。
  * 可选 GPU 硬件加速（Intel VAAPI / NVIDIA NVENC，仅 H.264/H.265 支持硬件编码）。
  * SQLite 持久化任务队列，重启不丢状态。
  * 内置轻量 HTTP 服务，提供 Web 管理界面与 JSON API。

用法：
  python3 autoproxy.py --config /path/config.json --var /path/var \
                       --web /path/index.html
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_NAME = "AutoProxy"
PROXY_DIR_NAME = "Proxy"
VAAPI_DEVICE = "/dev/dri/renderD128"

# 需要监控的视频扩展名（小写）
VIDEO_EXTS = {
    ".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".m2t", ".avi", ".mkv",
    ".mpg", ".mpeg", ".ts", ".wmv", ".flv", ".webm", ".m4v", ".3gp",
    ".braw", ".r3d", ".ari", ".dng",
}

# 编码格式参数表
# hw_enc: 该编码是否支持硬件编码器（None 表示仅 CPU，如 DNxHR/ProRes）
CODECS = {
    "dnxhr_lb": {
        "label": "DNxHR LB",
        "ext": ".mov",
        "hw_enc": None,
        "vargs": ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"],
        "aargs": ["-c:a", "pcm_s16le"],
    },
    "prores_proxy": {
        "label": "ProRes Proxy",
        "ext": ".mov",
        "hw_enc": None,
        "vargs": ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le"],
        "aargs": ["-c:a", "pcm_s16le"],
    },
    "h264": {
        "label": "H.264",
        "ext": ".mp4",
        "hw_enc": "vaapi",
        "vargs": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"],
        "hw_vargs": ["-c:v", "h264_vaapi", "-qp", "23"],
        "nvenc_vargs": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"],
        "aargs": ["-c:a", "aac", "-b:a", "192k"],
    },
    "h265": {
        "label": "H.265",
        "ext": ".mp4",
        "hw_enc": "vaapi",
        "vargs": ["-c:v", "libx265", "-preset", "veryfast", "-crf", "28", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"],
        "hw_vargs": ["-c:v", "hevc_vaapi", "-qp", "28"],
        "nvenc_vargs": ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "28"],
        "aargs": ["-c:a", "aac", "-b:a", "192k"],
    },
}

DEFAULT_CONFIG = {
    "watch_dirs": [],
    "codec": "dnxhr_lb",
    "resolution": "1080p",
    "port": 9080,
    "hwaccel": "auto",
    "scan_interval": 5,
    "stable_wait": 10,
    "workers": 1,
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    # 仅在前台终端（stdout 为 tty）时打印，避免 daemon 模式下与文件日志重复
    try:
        if sys.stdout.isatty():
            print(line, flush=True)
    except Exception:
        pass
    # 写入日志文件
    log_file = getattr(log, "_file", None)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    # 内存环形缓冲，供 API 读取
    buf = getattr(log, "_buf", None)
    if buf is not None:
        buf.append(line)
        if len(buf) > 200:
            del buf[0 : len(buf) - 200]


def init_logging(path):
    log._file = path
    log._buf = []
    if path:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)


def scale_filter(resolution, codec):
    if resolution == "1080p":
        return "scale=-2:1080"
    if resolution == "720p":
        return "scale=-2:720"
    if resolution == "half":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if resolution == "original":
        # DNxHR / ProRes 要求偶数尺寸，做一次安全的偶数化（偶数时等于恒等）
        if codec in ("dnxhr_lb", "prores_proxy"):
            return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        return None
    return None


# fnOS 及常见 Linux 系统中 ffmpeg/ffprobe 的固定位置（自检兜底，避免 PATH 精简时漏检）。
# fnOS 系统自带 /usr/bin/ffmpeg（已编译 VA-API），因此通常无需额外安装。
_COMMON_TOOL_DIRS = [
    "/usr/bin", "/usr/local/bin", "/opt/ffmpeg/bin", "/opt/ffmpeg",
    "/opt/ffmpeg-static/bin", "/opt/homebrew/bin",
]


def find_tool(names, extra_dirs=None):
    """按优先级查找可执行文件：内置目录 -> 常见系统目录 -> PATH。"""
    dirs = list(extra_dirs or [])
    dirs += _COMMON_TOOL_DIRS
    dirs += os.environ.get("PATH", "").split(os.pathsep)
    seen = set()
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        for name in names:
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def _authorized_dirs_from_env():
    """读取飞牛「授权目录」机制注入的环境变量。

    用户在「应用中心 → 应用设置 → 授权目录」中用飞牛文件选择器授权的目录，
    会通过 TRIM_DATA_ACCESSIBLE_PATHS（以 ':' 分隔）注入给应用进程。
    该值在进程启动时固定，运行中新增授权目录需重启应用才会生效。"""
    raw = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
    return [p for p in raw.split(":") if p.strip()]


def _merge_dirs(authorized, manual):
    """合并授权目录与手动目录，去重并保持顺序（授权目录在前）。"""
    seen, out = set(), []
    for p in list(authorized) + list(manual):
        if not p:
            continue
        key = os.path.normpath(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


try:
    class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """监听 Unix Socket 的 HTTP 服务器（供飞牛统一网关接入，网关负责登录态鉴权）。"""
        daemon_threads = True
except AttributeError:
    # 无 Unix Socket 支持的环境（如部分 Windows 构建）仅占位；实际部署运行在 fnOS（Linux）
    ThreadingUnixHTTPServer = None


# 统一网关前缀：飞牛网关转发时保留 /app/autoproxy 前缀，应用需自行剥离
GATEWAY_PREFIX = os.environ.get("AUTOPROXY_GATEWAY_PREFIX", "/app/autoproxy")


def strip_gateway_prefix(path):
    """兼容统一网关带前缀转发：剥掉 /app/autoproxy 前缀（若存在）。"""
    if GATEWAY_PREFIX and (path == GATEWAY_PREFIX or path.startswith(GATEWAY_PREFIX + "/")):
        rest = path[len(GATEWAY_PREFIX):]
        return rest if rest.startswith("/") else "/" + rest
    return path


# 不允许作为监控目录的系统顶层目录（防止监控/遍历敏感路径）
_SYSTEM_TOP_DIRS = {
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/proc", "/sys", "/dev", "/run", "/var", "/root", "/home", "/tmp",
    "/srv", "/opt", "/snap", "/lost+found",
}


def validate_watch_path(path):
    """校验用户提供的监控目录路径，防止路径穿越与系统敏感目录。"""
    if not path or not path.startswith("/"):
        return False, "路径必须是绝对路径"
    parts = [p for p in path.split("/") if p]
    if not parts:
        return False, "不能监控根目录"
    if ".." in parts or "." in parts:
        return False, "路径包含非法段（. 或 ..），已拒绝"
    top = "/" + parts[0]
    if top in _SYSTEM_TOP_DIRS:
        return False, "不允许监控系统目录：%s" % top
    return True, ""


def detect_hw():
    """检测可用的硬件加速能力（VAAPI / NVIDIA）。"""
    hw = {"vaapi": False, "nvidia": False}
    if os.path.exists(VAAPI_DEVICE):
        hw["vaapi"] = True
    if shutil.which("nvidia-smi"):
        hw["nvidia"] = True
    return hw


def probe_timecode(path, ffprobe):
    """尽力从源文件读取时间码，失败返回 None。"""
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=timecode:stream_tags=timecode:format_tags=timecode",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if re.match(r"^\d{2}:\d{2}:\d{2}[:;]\d{2}$", line):
                return line
    except Exception:
        pass
    return None


def hw_kind_for(codec, hw, hwaccel):
    """返回实际使用的硬件加速方式：'vaapi' | 'nvenc' | None。"""
    if hwaccel == "off" or not hw:
        return None
    cc = CODECS.get(codec, {})
    if not cc.get("hw_enc"):
        return None  # DNxHR / ProRes 无硬件编码器
    if cc["hw_enc"] == "vaapi" and hw.get("vaapi"):
        return "vaapi"
    if cc["hw_enc"] == "vaapi" and hw.get("nvidia"):
        return "nvenc"
    return None


def build_command(ffmpeg, src, dst, codec, resolution, timecode, hw=None, hwaccel="auto"):
    cc = CODECS[codec]
    vf = scale_filter(resolution, codec)
    kind = hw_kind_for(codec, hw, hwaccel)
    base = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "warning"]

    if kind == "vaapi":
        vf_parts = []
        if vf:
            vf_parts.append(vf)
        vf_parts += ["format=nv12", "hwupload"]
        cmd = base + ["-vaapi_device", VAAPI_DEVICE,
                      "-i", src, "-map", "0:v:0", "-map", "0:a?",
                      "-vf", ",".join(vf_parts)]
        cmd += cc["hw_vargs"]
    elif kind == "nvenc":
        cmd = base + ["-i", src, "-map", "0:v:0", "-map", "0:a?"]
        if vf:
            cmd += ["-vf", vf]
        cmd += cc["nvenc_vargs"]
    else:
        cmd = base + ["-i", src, "-map", "0:v:0", "-map", "0:a?"]
        if vf:
            cmd += ["-vf", vf]
        cmd += cc["vargs"]

    cmd += cc["aargs"]
    if timecode:
        cmd += ["-timecode", timecode]
    cmd += ["-map_metadata", "0", "-movflags", "+faststart", dst]
    return cmd


def proxy_path_for(src, codec):
    """代理文件放在源文件所在目录的 Proxy 子文件夹内（紧挨源文件，不镜像子目录结构）。"""
    src_dir = os.path.dirname(src)
    base = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(src_dir, PROXY_DIR_NAME, base + CODECS[codec]["ext"])


class JobDB:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "src TEXT UNIQUE NOT NULL,"
            "dst TEXT,"
            "status TEXT NOT NULL DEFAULT 'pending',"
            "error TEXT,"
            "created_at REAL,"
            "started_at REAL,"
            "finished_at REAL)"
        )
        self._conn.commit()

    def add_or_ignore(self, src, dst):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO jobs(src,dst,status,created_at) VALUES(?,?,?,?)",
                (src, dst, "pending", time.time()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def next_pending(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT id,src,dst FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='processing', started_at=? WHERE id=?",
                (time.time(), row[0]),
            )
            self._conn.commit()
            return {"id": row[0], "src": row[1], "dst": row[2]}

    def mark_done(self, job_id, dst):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='done', dst=?, finished_at=?, error=NULL WHERE id=?",
                (dst, time.time(), job_id),
            )
            self._conn.commit()

    def mark_failed(self, job_id, error):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                (error[:2000], time.time(), job_id),
            )
            self._conn.commit()

    def retry(self, job_id):
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='pending', error=NULL WHERE id=?",
                (job_id,),
            )
            self._conn.commit()

    def remove_finished(self, status):
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE status=?", (status,))
            self._conn.commit()

    def list_jobs(self, limit=200):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,src,dst,status,error,created_at,started_at,finished_at "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "src": r[1], "dst": r[2], "status": r[3],
                "error": r[4], "created_at": r[5], "started_at": r[6], "finished_at": r[7],
            })
        return out

    def stats(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM jobs GROUP BY status"
            ).fetchall()
        d = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        for status, n in rows:
            if status in d:
                d[status] = n
        return d


class Watcher(threading.Thread):
    """轮询多个目录，检测新增且写入完成的视频文件。"""

    def __init__(self, config, db, stop_event, authorized_dirs=None):
        super().__init__(daemon=True)
        self.config = config
        self.db = db
        self.stop = stop_event
        self.authorized_dirs = list(authorized_dirs or [])
        self._seen = {}   # src -> (size, since_epoch)
        self._wake = threading.Event()

    def rescan(self):
        self._wake.set()

    def _watch_dirs(self):
        return _merge_dirs(self.authorized_dirs, self.config.get("watch_dirs", []))

    def _stable(self, src):
        try:
            size = os.path.getsize(src)
        except OSError:
            return False
        prev = self._seen.get(src)
        now = time.time()
        if prev is None or prev[0] != size:
            self._seen[src] = (size, now)
            return False
        return (now - prev[1]) >= float(self.config.get("stable_wait", 10))

    def _iter_videos(self):
        exts = {e.lower() for e in VIDEO_EXTS}
        for watch in self._watch_dirs():
            if not os.path.isdir(watch):
                continue
            for root, dirs, files in os.walk(watch):
                dirs[:] = [d for d in dirs if d != PROXY_DIR_NAME and not d.startswith(".")]
                for f in files:
                    if f.startswith("."):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in exts:
                        continue
                    yield watch, os.path.join(root, f)

    def run(self):
        while not self.stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                log("watch error: %s" % e)
            self._wake.wait(timeout=float(self.config.get("scan_interval", 5)))
            self._wake.clear()

    def scan_once(self):
        codec = self.config.get("codec", "dnxhr_lb")
        for watch, src in self._iter_videos():
            if not self._stable(src):
                continue
            dst = proxy_path_for(src, codec)
            if os.path.exists(dst):
                self._seen.pop(src, None)
                continue
            if self.db.add_or_ignore(src, dst):
                log("queued: %s" % src)
            self._seen.pop(src, None)


class Worker(threading.Thread):
    def __init__(self, config, db, ffmpeg, ffprobe, hw, stop_event):
        super().__init__(daemon=True)
        self.config = config
        self.db = db
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.hw = hw
        self.stop = stop_event

    def run(self):
        while not self.stop.is_set():
            job = self.db.next_pending()
            if job is None:
                time.sleep(1)
                continue
            self._process(job)

    def _process(self, job):
        src = job["src"]
        dst = job["dst"]
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tc = probe_timecode(src, self.ffprobe)
            kind = hw_kind_for(self.config.get("codec", "dnxhr_lb"),
                               self.hw, self.config.get("hwaccel", "auto"))
            if kind:
                log("transcode start (硬件加速 %s): %s" % (kind, os.path.basename(src)))
            else:
                log("transcode start: %s" % os.path.basename(src))
            cmd = build_command(self.ffmpeg, src, dst,
                                self.config.get("codec", "dnxhr_lb"),
                                self.config.get("resolution", "1080p"), tc,
                                hw=self.hw, hwaccel=self.config.get("hwaccel", "auto"))
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                self.db.mark_done(job["id"], dst)
                log("done: %s" % dst)
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                self.db.mark_failed(job["id"], err)
                log("failed: %s -> %s" % (src, err[:500]))
        except Exception as e:
            self.db.mark_failed(job["id"], str(e))
            log("failed: %s -> %s" % (src, e))


class Handler(BaseHTTPRequestHandler):
    app = None  # 由 main 注入

    def log_message(self, *a):
        pass

    def _check_auth(self):
        """token 鉴权：仅在 TCP 监听模式下启用；Unix Socket 模式由飞牛统一网关鉴权。"""
        token = self.app.auth_token
        if not token:
            return True
        if self.headers.get("Authorization", "") == "Bearer " + token:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == token:
            return True
        return False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if not self._check_auth():
            self._json({"error": "unauthorized"}, 401)
            return
        path = strip_gateway_prefix(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/log":
            self._api_log()
        elif self._serve_static(path):
            pass
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            self._json({"error": "unauthorized"}, 401)
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = strip_gateway_prefix(u.path)
        if path == "/api/rescan":
            if self.app.watcher:
                self.app.watcher.rescan()
            self._json({"ok": True})
        elif path == "/api/retry":
            job_id = q.get("id", [""])[0]
            if job_id.isdigit():
                self.app.db.retry(int(job_id))
            self._json({"ok": True})
        elif path == "/api/clear":
            scope = q.get("scope", ["done"])[0]
            if scope in ("done", "failed", "pending"):
                self.app.db.remove_finished(scope)
            self._json({"ok": True})
        elif path == "/api/dirs/add":
            ok, msg = self.app.add_dir(q.get("path", [""])[0])
            self._json({"ok": ok, "message": msg})
        elif path == "/api/dirs/remove":
            ok, msg = self.app.remove_dir(q.get("path", [""])[0])
            self._json({"ok": ok, "message": msg})
        else:
            self._json({"error": "not found"}, 404)

    def _serve_index(self):
        path = self.app.web_index
        try:
            with open(path, "rb") as f:
                data = f.read()
            self._send(200, data, "text/html; charset=utf-8")
        except Exception as e:
            self._send(500, "web index missing: %s" % e, "text/plain; charset=utf-8")

    def _serve_static(self, path):
        """服务 web 目录下的静态文件（如 JS SDK），供前端 <script type=module> 引入。"""
        base = os.path.abspath(os.path.dirname(self.app.web_index))
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(base, rel))
        # 防目录穿越：确保解析后的路径仍在 web 目录内
        if not full.startswith(base) or not os.path.isfile(full):
            return False
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
            return True
        except Exception:
            return False

    def _api_status(self):
        cfg = self.app.config
        codec = cfg.get("codec", "dnxhr_lb")
        hwaccel = cfg.get("hwaccel", "auto")
        manual = cfg.get("watch_dirs", [])
        auth = self.app.authorized_dirs
        dirs = _merge_dirs(auth, manual)
        auth_norm = {os.path.normpath(a) for a in auth}
        hw = self.app.hw
        hw_active = hw_kind_for(codec, hw, hwaccel)
        self._json({
            "app": APP_NAME,
            "config": {
                "watch_dirs": manual,
                "codec": codec,
                "resolution": cfg.get("resolution", "1080p"),
                "port": cfg.get("port", 9080),
                "hwaccel": hwaccel,
                "scan_interval": cfg.get("scan_interval", 5),
            },
            "authorized_dirs": auth,
            "dirs": [
                {
                    "path": d,
                    "source": "authorized" if os.path.normpath(d) in auth_norm else "manual",
                    "exists": os.path.isdir(d),
                    "readable": os.path.isdir(d) and os.access(d, os.R_OK),
                }
                for d in dirs
            ],
            "runtime": {
                "ffmpeg": self.app.ffmpeg or None,
                "ffprobe": self.app.ffprobe or None,
                "ffmpeg_ok": bool(self.app.ffmpeg),
            },
            "hw": {
                "vaapi": hw.get("vaapi", False),
                "nvidia": hw.get("nvidia", False),
                "active": hw_active,
                "codec_hw_capable": bool(CODECS.get(codec, {}).get("hw_enc")),
            },
            "stats": self.app.db.stats(),
            "jobs": self.app.db.list_jobs(200),
        })

    def _api_log(self):
        buf = getattr(log, "_buf", [])
        self._json({"log": buf[-100:]})


class App:
    def __init__(self, config_path, var_dir, web_index, socket_path=None, auth_token=None):
        self.config_path = config_path
        self.var_dir = var_dir
        self.web_index = web_index
        self.socket_path = socket_path or os.environ.get("AUTOPROXY_SOCKET", "")
        self.auth_token = auth_token or os.environ.get("AUTOPROXY_AUTH_TOKEN", "")
        self.config = self._load_config()
        self.authorized_dirs = _authorized_dirs_from_env()
        self.stop = threading.Event()
        os.makedirs(var_dir, exist_ok=True)

        db_path = os.path.join(var_dir, "jobs.db")
        self.db = JobDB(db_path)

        extra = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")]
        self.ffmpeg = find_tool(["ffmpeg"], extra)
        self.ffprobe = find_tool(["ffprobe"], extra)
        if not self.ffmpeg:
            log("WARNING: ffmpeg 未找到，转码将失败。")

        self.hw = detect_hw()

        self.watcher = Watcher(self.config, self.db, self.stop, self.authorized_dirs)
        self.workers = []
        for _ in range(int(self.config.get("workers", 1))):
            self.workers.append(Worker(self.config, self.db, self.ffmpeg, self.ffprobe, self.hw, self.stop))

    def _load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v not in (None, "")})
        except Exception as e:
            log("config load failed (%s), using defaults" % e)
        # 兼容旧版单目录 watch_dir -> 新版数组 watch_dirs
        if not cfg.get("watch_dirs"):
            wd = cfg.get("watch_dir", "")
            cfg["watch_dirs"] = [wd] if wd else []
        return cfg

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            log("save config failed: %s" % e)
            return False

    def add_dir(self, path):
        path = (path or "").strip()
        if not path:
            return False, "目录不能为空"
        ok, err = validate_watch_path(path)
        if not ok:
            return False, err
        dirs = self.config.setdefault("watch_dirs", [])
        if path in dirs:
            return False, "该目录已存在"
        dirs.append(path)
        if self._save_config():
            if self.watcher:
                self.watcher.rescan()
            log("add watch dir: %s" % path)
            return True, "已添加"
        return False, "保存配置失败"

    def remove_dir(self, path):
        path = (path or "").strip()
        dirs = self.config.setdefault("watch_dirs", [])
        if path not in dirs:
            return False, "目录不存在"
        dirs.remove(path)
        if self._save_config():
            log("remove watch dir: %s" % path)
            return True, "已删除"
        return False, "保存配置失败"

    def start(self):
        if self.ffmpeg:
            log("ffmpeg: %s" % self.ffmpeg)
        log("授权目录(飞牛): %s" % (self.authorized_dirs or "(无)"))
        log("手动目录: %s" % (self.config.get("watch_dirs", []) or "(无)"))
        log("有效监控目录: %s" % (_merge_dirs(self.authorized_dirs, self.config.get("watch_dirs", [])) or "(无)"))
        log("codec=%s resolution=%s hwaccel=%s" % (
            self.config.get("codec"), self.config.get("resolution"), self.config.get("hwaccel", "auto")))
        if self.hw.get("vaapi"):
            log("硬件加速: VAAPI 可用")
        if self.hw.get("nvidia"):
            log("硬件加速: NVIDIA 可用")
        self.watcher.start()
        for w in self.workers:
            w.start()
        Handler.app = self
        if self.socket_path:
            # Unix Socket 模式：接入飞牛统一网关，不暴露 TCP 端口，鉴权由飞牛网关完成
            if ThreadingUnixHTTPServer is None:
                log("ERROR: 当前运行环境不支持 Unix Socket，无法接入统一网关")
                return
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
            self.httpd = ThreadingUnixHTTPServer(self.socket_path, Handler)
            self.httpd.timeout = 0.5
            log("web UI listening on unix socket %s (飞牛统一网关鉴权)" % self.socket_path)
        else:
            # TCP 回环模式：仅监听 127.0.0.1，不暴露公网；带 token 时启用鉴权
            port = int(self.config.get("port", 9080))
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            self.httpd.daemon_threads = True
            self.httpd.timeout = 0.5
            if self.auth_token:
                log("web UI listening on http://127.0.0.1:%s (token 鉴权已启用)" % port)
            else:
                log("WARNING: web UI 以回环端口 %s 监听且未设置 token，仅限本机访问，请勿暴露到公网" % port)
        while not self.stop.is_set():
            self.httpd.handle_request()
        self.httpd.server_close()
        if self.socket_path and os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def shutdown(self, *a):
        # 仅设置退出标志，让 start() 的主循环自行退出，绝不在信号处理器里做阻塞操作
        self.stop.set()


def main():
    ap = argparse.ArgumentParser(description="AutoProxy daemon")
    ap.add_argument("--config", required=True, help="config.json 路径")
    ap.add_argument("--var", required=True, help="运行时数据目录")
    ap.add_argument("--web", required=True, help="web index.html 路径")
    ap.add_argument("--socket", help="Unix Socket 路径（接入飞牛统一网关，不监听 TCP 端口）")
    ap.add_argument("--auth-token", help="TCP 监听模式下的鉴权 token（Unix Socket 模式无需）")
    args = ap.parse_args()

    init_logging(os.path.join(args.var, "autoproxy.log"))
    app = App(args.config, args.var, args.web,
              socket_path=args.socket, auth_token=args.auth_token)
    signal.signal(signal.SIGTERM, app.shutdown)
    signal.signal(signal.SIGINT, app.shutdown)
    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()


if __name__ == "__main__":
    main()
