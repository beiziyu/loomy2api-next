#!/usr/bin/env python3
"""构建自检：把页面里的 <script> 抽出来交给 node --check，防止再次出现 JS 语法错误。"""
import re, shutil, subprocess, sys, tempfile

sys.path.insert(0, ".")
import loomy_registrar_gui as g

page = g.PAGE
m = re.search(r"<script>(.*?)</script>", page, re.S)
if not m:
    print("❌ 页面里没有 <script>")
    sys.exit(1)
js = m.group(1)
if shutil.which("node"):
    path = tempfile.mktemp(suffix=".js")
    open(path, "w").write(js)
    res = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if res.returncode != 0:
        print("❌ JS 语法错误:\n" + res.stderr[:600])
        sys.exit(1)
    print("✅ 页面 JS 语法 OK（%d 字符）" % len(js))
else:
    print("⚠️ 未安装 node，跳过 JS 校验")
