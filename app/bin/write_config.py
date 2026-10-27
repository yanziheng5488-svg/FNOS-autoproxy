#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取安装/配置向导产生的环境变量，写入 config.json。

- 监控目录不再由向导手填：素材目录通过飞牛「应用设置 → 授权目录」用文件选择器
  授权，应用运行时经 TRIM_DATA_ACCESSIBLE_PATHS 自动读取；本脚本只保留用户
  在 Web 界面里手动添加的目录（watch_dirs），不覆盖它们。
- 编码/分辨率/硬件加速来自向导。"""
import json
import os
import sys


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TRIM_PKGETC", ".") + "/config.json"
    codec = os.environ.get("wizard_codec", "dnxhr_lb")
    res = os.environ.get("wizard_resolution", "1080p")
    port = os.environ.get("wizard_port", "9080")
    hwaccel = os.environ.get("wizard_hwaccel", "auto")
    try:
        port = int(port)
    except ValueError:
        port = 9080

    # 保留已有的 watch_dirs（用户在 Web 界面手动新增的目录），其余用默认/向导值
    watch_dirs = []
    if os.path.exists(out):
        try:
            with open(out, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                if old.get("watch_dirs"):
                    watch_dirs = [d for d in old["watch_dirs"] if d]
                elif old.get("watch_dir"):  # 兼容旧版单目录字段 watch_dir
                    watch_dirs = [old["watch_dir"]]
        except Exception:
            pass

    cfg = {
        "watch_dirs": watch_dirs,
        "codec": codec,
        "resolution": res,
        "port": port,
        "hwaccel": hwaccel,
    }
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("AutoProxy config written to %s" % out)


if __name__ == "__main__":
    main()
