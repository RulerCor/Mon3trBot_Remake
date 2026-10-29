# -*- coding: utf-8 -*-
"""build_dist.py —— 出「正式发行版」：解压即用，不用安装 Python / AstrBot / 依赖。

取代了旧的 build_portable.py（便携版体系已废弃，不要再用那套）。
两者最大的区别是**放什么**：便携版塞了一堆项目文档、模拟器、维护脚本；
正式发行版只放「能跑起来所必需的东西」+「安装包」。

发行版结构
----------
    Mon3trBot_Remake-vX.Y.Z/
    ├── 启动 Mon3trBot.bat      一键启动（% ~dp0 相对定位，解压到哪都能跑）
    ├── 停止 Mon3trBot.bat
    ├── 使用说明.md
    ├── akasha/                  ★ 桥接【完整可运行副本】（含 .venv）
    ├── astrbot/                 ★ AstrBot【完整副本】（含 .venv / 三人格 / 普通知识库）
    │   └── 启动 AstrBot.bat     目录内相对路径启动脚本（单独调试用）
    ├── python/                  免安装 Python 基座（两个 venv 都指向它）
    ├── installers/              WeFlow / 微信 的**安装包**（exe）
    └── BUILD_INFO.md

三条铁律（出包前必须成立，否则脚本中止）
----------------------------------------
1. **绝不带个人隐私**：API Key / 面板口令 / jwt_secret / 真实 wxid / 群名 / 人名 /
   本机绝对路径，全部剥掉或替换；最后跑一遍整包审计，**零残留才出包**。
2. **人设是软件资产，要保留**：`data_v4.db` 只保留 `personas` 表（三个人格），
   其余表（聊天历史、会话映射、定时任务、API Key）全清。
3. **普通知识库文档保留、向量知识库删除**：
   - 保留 `kb_docs/*.md`（收件人可能要看/自己重建索引）
   - 删除 `data/knowledge_base/*/{doc.db,index.faiss}`（向量索引体积大且含本机特征）

关于 venv 可移植
----------------
Windows 的 venv 靠 `pyvenv.cfg` 里的 `home` 找基座解释器。启动器每次启动都用
「包内 python\\ 的当前绝对路径」重写它 —— 所以包能解压到任意目录。
但**别把 python\\ 单独挪走或改名**。

用法
----
    python scripts/build_dist.py                    # 构建 + 出 zip
    python scripts/build_dist.py --no-zip           # 只构建到构建目录
    python scripts/build_dist.py --stage D:\\tmp     # 换构建目录（务必用短路径）

构建目录默认 `build/`（项目内，路径最短的可用位置）：venv 有近 5 万个小文件，
放在深目录会撞 Windows 的 260 字符路径上限（本机未开长路径支持）。
打包成功后**自动删除** stage 目录，只留 `release/dist/*.zip`，不在项目外留残留。
（真撞上限时用 `--stage` 换更短的盘符根目录，例如 `--stage C:\\_mb_dist`。）
"""

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CODE_DIR = os.path.join(ROOT, "wechat-weflow-bridge-ob11")
RT_ASTRBOT = os.path.join(ROOT, "runtime", "astrbot")
RT_BRIDGE = os.path.join(ROOT, "runtime", "bridge")
VENDOR = os.path.join(ROOT, "vendor")
RELEASE_DIST = os.path.join(ROOT, "release", "dist")
DEFAULT_STAGE = os.path.join(ROOT, "build")

# 项目名称（2026-10-19 起）—— 发行版 zip 与包内目录名都用它
PROJECT_CODE = "Mon3trBot_Remake"

ROBOCOPY = shutil.which("robocopy") or r"C:\Windows\System32\robocopy.exe"

# ── 安装包 ──────────────────────────────────────────────────────────
# 收件人自己装微信/WeFlow 容易装错版本，所以把安装包一起放进去。
# 约定：把安装包放进 scripts/installers_src/（或项目根的 installers_src/），
# 文件名任取，脚本按关键字自动识别归类。
INSTALLER_SRC_DIRS = [
    os.path.join(ROOT, "installers_src"),
    os.path.join(ROOT, "scripts", "installers_src"),
]
# 识别规则：(发行版内的目录名, 匹配文件名的关键字)
INSTALLER_RULES = [
    ("WeFlow", ("weflow",)),
    ("微信", ("weixin", "wechat", "微信")),
]
# 这些目录里也可能直接放着安装包（用户习惯），作为补充搜索位置
INSTALLER_EXTRA_DIRS = [
    os.path.join(os.path.expanduser("~"), "Desktop"),
    os.path.join(os.path.expanduser("~"), "Downloads"),
]

SKIP_DIR = {".git", "__pycache__", ".idea", ".vscode", "node_modules"}
SKIP_FILE_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".pid")
SKIP_FILE_NAMES = {".kb_api_key", "astrbot.lock", "bridge.pid"}

# 桥接要从源码副本带过去的文件（发布副本才是"干净源"）
# ⚠️ 新增桥接模块时必须同步加到这里，否则发行版会缺文件
#    （2026-09-18 踩过：astrbot_ctl.py 漏加 → 面板「重启 AstrBot」按钮在收件人那里直接报错）
BRIDGE_FILES = ["main.py", "bridge_core.py", "message_source.py",
                "wx_msg_parser.py", "ob_client.py", "ob_protocol.py",
                "people.py", "senders.py", "state.py", "uia_sender.py",
                "web_panel.py", "config.py", "astrbot_ctl.py", "exit_reason.py",
                "requirements.txt",
                "LICENSE", "README.md", "config.example.json", "VERSION"]

# AstrBot 运行副本里要带的顶层文件
ASTRBOT_FILES = ["run_astrbot.py"]

# 兜底：这些串**绝对不允许**出现在发行版里（除了被白名单放行的占位符）
# 前三条是本机绝对路径与 Windows 真实姓名 —— 最要命的一类泄漏。
# ⚠️ RulerCordelius 曾在此名单，2026-09-18 起它是项目代号
# 只对通用路径占位符做替换，真实账号标识仍会被审计拦截。
# 真实姓名 Junqin Zhao 仍是隐私，继续拦截。
HARD_LITERALS = [
    "Junqin Zhao",
    r"C:\Users\Junqin Zhao",
    "C:/Users/Junqin Zhao",
]


def neutralize_venv_paths(stage):
    """把两个 venv 里写死的本机绝对路径 / Windows 用户名改干净。

    为什么要单独做这一步：venv 的 `pyvenv.cfg` 与 `Scripts/activate*` 里
    记着**创建时的本机绝对路径**（`C:\\Users\\<用户名>\\...`），
    实测这些文件逃过了基于"真实标识符"的脱敏表（表是从运行数据生成的，
    不含本机路径），最后是靠硬检查才拦下来的。

    处理方式：
      · `pyvenv.cfg` → 写成占位 `home`；启动器每次启动都会用包内实际路径重写它，
        所以这里写什么都不影响运行。
      · `activate*` → 做文本替换（只在**我们自己的** venv 里做，第三方包不碰）。
      · 顺手删掉 `pyvenv.cfg.bak_*`（WorkBuddy 留下的备份，只会带路径）。
    """
    fixed = []
    user = os.environ.get("USERNAME", "")
    patterns = []
    if user:
        patterns += [(f"C:\\Users\\{user}", r"C:\AkashaRuntime"),
                     (f"C:/Users/{user}", "C:/AkashaRuntime"),
                     (user, "AkashaUser")]
    for base, dirs, fs in os.walk(stage):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in fs:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, stage)
            # 清掉遗留备份
            if f.startswith("pyvenv.cfg.bak"):
                os.remove(p)
                fixed.append(f"{rel}（删除）")
                continue
            if f == "pyvenv.cfg":
                io.open(p, "w", encoding="utf-8", newline="\n").write(
                    "home = C:\\AkashaRuntime\\python\r\n"
                    "include-system-site-packages = false\r\n"
                    "version = 3.13.14\r\n")
                fixed.append(rel)
                continue
            # activate* / 其他 Scripts 小脚本 → 文本替换
            if not (f.startswith("activate") or f.endswith(".py")):
                continue
            try:
                s = io.open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            orig = s
            for old, new in patterns:
                if old in s:
                    s = s.replace(old, new)
            if s != orig:
                io.open(p, "w", encoding="utf-8", newline="").write(s)
                fixed.append(rel)
    return fixed


def log(msg):
    print(msg, flush=True)


def hr(t=""):
    log("=" * 74 if not t else f"\n{'─' * 66}\n{t}\n{'─' * 66}")


# 常见密钥前缀 —— 用来在配置里"兜底"抓漏网的 key。
# ⚠️ 实测踩过的坑：`sk_` / `sk-` 太宽了 —— 会命中 `sk_feed`（变量名）、
# `sk_id`（task id）、`sk-exception`（SPDX 许可证标识），
# 第一版因此报了 782 处全假阳性。所以改成"前缀 + 长度 + 字符构成"三重约束：
# 真密钥是长串高熵字符串，代码标识符不会长成那样。
SECRET_PATTERNS = [
    # OpenRouter / OpenAI 系
    re.compile(r"sk-(?:or-|cp-|tr-|proj-)?[A-Za-z0-9_\-]{32,}"),
    # NVIDIA
    re.compile(r"nvapi-[A-Za-z0-9_\-]{32,}"),
    # Google
    re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),
    # GitHub
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    # HuggingFace
    re.compile(r"hf_[A-Za-z0-9]{30,}"),
    # GitLab
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),
    # 通用 Bearer <长串>
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{30,}"),
]

# 命中这些词的一律当占位符/示例，放过
SECRET_ALLOW = ("your", "example", "xxx", "placeholder", "填入", "示例",
                "yourkey", "<", "config.", "{", "self.", "os.environ",
                "${", "%s", "***")


def _looks_like_secret(s: str) -> bool:
    """粗判一段字符串是不是密钥（宁可误杀，不可漏放）。"""
    if not isinstance(s, str) or len(s) < 20:
        return False
    return any(p.search(s) for p in SECRET_PATTERNS)


def scan_for_secrets(root):
    """全包扫一遍"看起来像密钥"的串，作为脱敏之外的独立兜底。

    为什么还要这一道：字段级清理总是会漏（实测就漏了 custom_headers）。
    这里不看字段名、只看内容特征，所以能兜住结构化清理覆盖不到的位置。

    只扫我们自己的文件（源码 / 配置 / 文档），**不扫第三方 venv** ——
    第三方库里本来就有大量形如密钥的测试串与许可证标识，扫了全是噪音。
    """
    hits = []
    SKIP_EXT = (".pyc", ".pyd", ".dll", ".exe", ".pak", ".bin", ".faiss",
                ".db", ".zip", ".node", ".asar", ".png", ".jpg", ".ico",
                ".ttf", ".woff", ".woff2", ".dat", ".msi")
    for base, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".venv", "site-packages",
                                "node_modules")]
        for f in fs:
            p = os.path.join(base, f)
            if f.lower().endswith(SKIP_EXT):
                continue
            try:
                if os.path.getsize(p) > 8 * 1024 * 1024:
                    continue
                s = io.open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for pat in SECRET_PATTERNS:
                for m in pat.finditer(s):
                    frag = m.group(0)
                    low = frag.lower()
                    # 占位符 / 示例 / 模板变量 放过
                    if any(w in low for w in SECRET_ALLOW):
                        continue
                    # 密钥本体应高熵：要求同时含字母和数字，且够长
                    if len(frag) < 32:
                        continue
                    if not (any(c.isdigit() for c in frag)
                            and any(c.isalpha() for c in frag)):
                        continue
                    hits.append((os.path.relpath(p, root), frag[:50]))
    return hits


# 微信媒体指纹（引用图 / 表情包）——这些是**真实账号的媒体内容标识**，
# 与 wxid/群名/人名同属隐私，但脱敏表是从运行数据的身份字段生成的，
# 覆盖不到藏在 docstring 里的示例值。实测 v1.9.3 出包时漏了 4 处。
# 这里只看"字段名 + 高熵值"的特征，不硬编码任何真实串。
MEDIA_FP_PATTERNS = [
    # <img ... md5="32hex" ...> / cdnbigimgurl="..." / aeskey="..." / svrid
    re.compile(r"""(?:md5|emoticonmd5)\s*[=:]\s*["\']?([0-9a-fA-F]{32})\b"""),
    re.compile(r"""(?:cdn(?:big|mid|thumb)(?:img)?url|aeskey|cdnthumbaeskey)\s*=\s*["\']([0-9a-fA-F]{16,})["\']"""),
    re.compile(r"""<svrid>\s*(\d{12,})\s*</svrid>"""),
    # 标签形式：<emoticonmd5>32hex</emoticonmd5>
    re.compile(r"""<(?:emoticon)?md5>\s*([0-9a-fA-F]{32})\s*</"""),
    # <emojiinfo>base64...</emojiinfo>（解出来是带 token 的 CDN 直链）
    re.compile(r"""<emojiinfo>\s*([A-Za-z0-9+/=]{24,})\s*</emojiinfo>"""),
]

# 合成/占位值放过（夹具与文档里刻意使用的样例）
MEDIA_FP_ALLOW = ("example", "placeholder", "your", "xxx", "sample",
                  "deadbeef", "0123456789abcdef", "<", "hex")


def scan_media_fingerprints(root):
    """扫自己文件里的真实媒体指纹（md5 / aeskey / cdn url / emojiinfo）。"""
    hits = []
    SKIP_EXT = (".pyc", ".pyd", ".dll", ".exe", ".pak", ".bin", ".faiss",
                ".db", ".zip", ".node", ".asar", ".png", ".jpg", ".ico",
                ".ttf", ".woff", ".woff2", ".dat", ".msi")
    for base, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".venv", "site-packages",
                                "node_modules")]
        for f in fs:
            if f.lower().endswith(SKIP_EXT):
                continue
            p = os.path.join(base, f)
            try:
                if os.path.getsize(p) > 8 * 1024 * 1024:
                    continue
                s = io.open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for pat in MEDIA_FP_PATTERNS:
                for m in pat.finditer(s):
                    frag = m.group(1)
                    low = frag.lower()
                    if any(w in low for w in MEDIA_FP_ALLOW):
                        continue
                    hits.append((os.path.relpath(p, root), frag[:50]))
    return hits


def version():
    with io.open(os.path.join(CODE_DIR, "VERSION"), encoding="utf-8") as f:
        return f.read().strip() or "0.0.0"


def base_python_dir():
    """定位基座解释器目录（优先项目内 vendor/python，自包含）。"""
    v = os.path.join(VENDOR, "python")
    if os.path.isfile(os.path.join(v, "python.exe")):
        return v
    cfg = os.path.join(RT_BRIDGE, ".venv", "pyvenv.cfg")
    if os.path.isfile(cfg):
        for line in io.open(cfg, encoding="utf-8", errors="replace"):
            if line.lower().startswith("home"):
                p = line.split("=", 1)[1].strip()
                if os.path.isdir(p):
                    log("  ⚠️ 未找到 vendor/python，回落到 pyvenv.cfg 记录的基座")
                    return p
    raise SystemExit("❌ 找不到基座 Python（vendor/python 与 pyvenv.cfg 都不可用）")


# ───────────────────────── 文件操作 ─────────────────────────

def rmtree_fast(path):
    """快速清空目录（几万个小文件时 shutil.rmtree 极慢）。"""
    if not os.path.isdir(path):
        return
    empty = os.path.join(os.path.dirname(path.rstrip("\\/")), "_empty_clean")
    os.makedirs(empty, exist_ok=True)
    subprocess.run([ROBOCOPY, empty, path, "/MIR", "/NFL", "/NDL", "/NJH",
                    "/NJS", "/NP", "/R:0", "/W:0"], capture_output=True)
    try:
        os.rmdir(empty)
    except OSError:
        pass
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# Windows MAX_PATH：260 含结尾 NUL，所以可用 259。本机 LongPathsEnabled=0。
MAX_PATH = 259


def _longest_rel(root):
    """返回 root 下最长的相对路径长度（不含 root 本身，含前导分隔符）。"""
    worst = 0
    for base, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR]
        for f in fs:
            worst = max(worst, len(base) - len(root) + 1 + len(f))
    return worst


def check_path_budget(stage_root, src_dirs):
    """复制前先算最坏路径长度，撞 MAX_PATH 就直接拒绝，别让 robocopy 半途报错。"""
    worst_rel = max((_longest_rel(d) for d in src_dirs if os.path.isdir(d)),
                    default=0)
    worst = len(stage_root) + worst_rel
    log(f"  路径预算: stage {len(stage_root)} + 最长相对 {worst_rel}"
        f" = {worst} 字符（上限 {MAX_PATH}）")
    if worst > MAX_PATH:
        raise SystemExit(
            f"❌ 路径太长（{worst} > {MAX_PATH}）：{stage_root}\n"
            f"   请用更短的构建目录，例如 `--stage C:\\_mb_dist`"
            f"（或开启 Windows 长路径支持）。")


def _walk_stats(root):
    n = b = 0
    for base, _dirs, fs in os.walk(root):
        for f in fs:
            try:
                b += os.path.getsize(os.path.join(base, f))
                n += 1
            except OSError:
                pass
    return n, b


def copytree(src, dst, skip=SKIP_DIR, extra_xd=(), raw=False, extra_skip_names=()):
    """robocopy /MT 多线程复制（几万小文件时比 shutil 快一个数量级）。"""
    if not os.path.isdir(src):
        return 0, 0
    os.makedirs(dst, exist_ok=True)
    cmd = [ROBOCOPY, src, dst, "/E", "/MT:32", "/NFL", "/NDL", "/NJH", "/NJS",
           "/NP", "/R:1", "/W:1"]
    xd = list(extra_xd)
    if not raw:
        xd += list(skip)
    if xd:
        cmd += ["/XD"] + sorted(set(xd))
    if not raw:
        xf = set(SKIP_FILE_NAMES) | set(extra_skip_names) | {
            "*.pyc", "*.pyo", "*.log", "*.tmp", "*.pid"}
        cmd += ["/XF"] + sorted(xf)
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode >= 8:  # robocopy 0~7 都算成功
        log(f"    ⚠️ robocopy 返回 {r.returncode}: {(r.stderr or b'')[:200]!r}")
    return _walk_stats(dst)


def prune_empty(root):
    for base, _dirs, _fs in os.walk(root, topdown=False):
        try:
            if not os.listdir(base):
                os.rmdir(base)
        except OSError:
            pass


# ───────────────────────── 安装包 ─────────────────────────

def find_installers():
    """收集安装包 → {"WeFlow": path, "微信": path}。

    找不到不报错（只告警）——微信旧版安装包腾讯已下架，用户可能确实没有。
    """
    found = {}
    cands = []
    for d in INSTALLER_SRC_DIRS + INSTALLER_EXTRA_DIRS:
        if not os.path.isdir(d):
            continue
        for base, dirs, fs in os.walk(d):
            # 别扫进别人的大目录深处
            dirs[:] = [x for x in dirs if x.lower() not in
                       {"node_modules", "__pycache__", ".git", "windows",
                        "program files", "program files (x86)"}]
            if base.count(os.sep) - d.count(os.sep) > 2:
                dirs[:] = []
                continue
            for f in fs:
                if f.lower().endswith((".exe", ".msi")) and "setup" in f.lower() \
                        or f.lower().endswith((".exe", ".msi")):
                    cands.append(os.path.join(base, f))
    for key, kws in INSTALLER_RULES:
        if key in found:
            continue
        for p in cands:
            low = os.path.basename(p).lower()
            if any(k in low for k in kws):
                # 排除卸载器/主程序本体，只认安装包
                if "uninstall" in low:
                    continue
                found[key] = p
                break
    return found


def copy_installers(dst_dir, found):
    os.makedirs(dst_dir, exist_ok=True)
    lines = []
    for key in ("WeFlow", "微信"):
        p = found.get(key)
        if not p:
            lines.append(f"- ❌ {key}：**未找到安装包**（请自行安装）")
            continue
        target = os.path.join(dst_dir, os.path.basename(p))
        shutil.copy2(p, target)
        mb = os.path.getsize(p) / 1024 / 1024
        lines.append(f"- ✅ {key}：`{os.path.basename(p)}`（{mb:.0f} MB）")
        log(f"  ✔ {key} 安装包 → {os.path.basename(p)}（{mb:.0f} MB）")
    return lines


# ───────────────────────── 脱敏 ─────────────────────────

def sanitize_cmd_config(astrbot_data):
    """剥掉 AstrBot 配置里的全部密钥 / 口令 / 真实 ID。"""
    p = os.path.join(astrbot_data, "cmd_config.json")
    if not os.path.isfile(p):
        return []
    d = json.load(io.open(p, encoding="utf-8-sig"))
    stripped = []

    # ① provider 的 api key 全清，并全部置为未启用
    #    ⚠️ 实测踩过的坑：key 不只躺在 `key` 字段里。MiniMax 那类服务把 key
    #    写在 `custom_headers.Authorization = "Bearer sk-..."`，
    #    早期版本只清 key/api_key，结果**整条 key 原样进了发行版**。
    #    所以这里对 custom_headers 做一次"敏感头"扫描，而不是逐个字段硬编码。
    SENSITIVE_HEADER_TOKENS = ("auth", "key", "token", "secret", "cookie",
                               "credential", "password", "session")
    for i, src in enumerate(d.get("provider_sources", [])):
        if not isinstance(src, dict):
            continue
        for k in ("key", "embedding_api_key", "api_key"):
            if src.get(k):
                src[k] = ""
                stripped.append(f"provider_sources[{i}].{k}")
        # custom_headers 里的敏感头一律清空
        ch = src.get("custom_headers")
        if isinstance(ch, dict):
            for hk in list(ch):
                if any(t in hk.lower() for t in SENSITIVE_HEADER_TOKENS):
                    if ch[hk]:
                        ch[hk] = ""
                        stripped.append(f"provider_sources[{i}].custom_headers.{hk}")
        # 兜底：任何字符串字段里如果还带着明显的密钥前缀，直接清掉
        for fk, fv in list(src.items()):
            if isinstance(fv, str) and _looks_like_secret(fv):
                src[fk] = ""
                stripped.append(f"provider_sources[{i}].{fk}（兜底：疑似密钥）")
    for i, prov in enumerate(d.get("provider", [])):
        if isinstance(prov, dict) and prov.get("enable"):
            prov["enable"] = False
            stripped.append(f"provider[{i}] 已停用")

    # ② 面板凭据
    dash = d.get("dashboard")
    if isinstance(dash, dict):
        for k in ("password", "pbkdf2_password", "jwt_secret",
                  "password_storage_upgraded", "password_change_required",
                  "trusted_devices", "session_secret"):
            if k in dash:
                dash.pop(k, None)
                stripped.append(f"dashboard.{k}")
        dash["username"] = "admin"

    # ③ 真实 ID / 白黑名单
    for key, val in (("admins_id", []),
                     ("platform_settings.id_whitelist", []),
                     ("platform_settings.id_whitelist_enable", False),
                     ("provider_ltm_settings.active_reply.whitelist", []),
                     ("provider_ltm_settings.active_reply.blacklist", []),
                     ("provider_ltm_settings.active_reply.enable", False)):
        cur = d
        parts = key.split(".")
        ok = True
        for part in parts[:-1]:
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                ok = False
                break
        if ok and isinstance(cur, dict) and parts[-1] in cur:
            cur[parts[-1]] = val
            stripped.append(key)

    io.open(p, "w", encoding="utf-8", newline="\n").write(
        json.dumps(d, ensure_ascii=False, indent=2))
    return stripped


def sanitize_db(astrbot_data):
    """data_v4.db：**只留 personas 表**（三个人格 = 软件资产），其余全清。"""
    p = os.path.join(astrbot_data, "data_v4.db")
    if not os.path.isfile(p):
        return [], 0
    for suf in ("-wal", "-shm"):
        f = p + suf
        if os.path.exists(f):
            os.remove(f)
    c = sqlite3.connect(p)
    cleared = []
    KEEP = {"personas"}
    try:
        tables = [r[0] for r in c.execute(
            "select name from sqlite_master where type='table'")]
        for t in tables:
            if t in KEEP or t.startswith("sqlite_"):
                continue
            try:
                n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
                if n:
                    c.execute(f'delete from "{t}"')
                    cleared.append(f"{t}({n})")
            except Exception:
                pass
        c.commit()
        c.execute("VACUUM")
        # 数一下人格还剩几个
        try:
            n_persona = c.execute("select count(*) from personas").fetchone()[0]
        except Exception:
            n_persona = 0
    finally:
        c.close()
    return cleared, n_persona


def drop_vector_kb(astrbot_data):
    """删除**向量知识库**文件（doc.db / index.faiss）。

    普通知识库文档（kb_docs/*.md）保留；这里删的是向量索引本体。
    """
    kb = os.path.join(astrbot_data, "knowledge_base")
    removed = []
    if not os.path.isdir(kb):
        return removed
    for name in os.listdir(kb):
        d = os.path.join(kb, name)
        if not os.path.isdir(d):
            if name.lower().endswith((".db", ".faiss", ".db-wal", ".db-shm")):
                os.remove(d)
                removed.append(name)
            continue
        for f in os.listdir(d):
            if f.lower().endswith((".faiss", ".db", ".db-wal", ".db-shm")):
                fp = os.path.join(d, f)
                sz = os.path.getsize(fp) / 1024 / 1024
                os.remove(fp)
                removed.append(f"{name}/{f} ({sz:.1f}MB)")
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return removed


def sanitize_bridge_config(bridge_dir):
    """桥接 config.json：用示例模板重建，绝不继承 token / wxid / 昵称。"""
    tpl = os.path.join(CODE_DIR, "config.example.json")
    if not os.path.isfile(tpl):
        return False
    cfg = json.load(io.open(tpl, encoding="utf-8"))
    cur_p = os.path.join(RT_BRIDGE, "config.json")
    if os.path.isfile(cur_p):
        cur = json.load(io.open(cur_p, encoding="utf-8"))
        # 只继承"结构与行为"类配置
        for k in ("send_method", "buffer_seconds", "group_reply_mode",
                  "image_caption_provider", "image_caption_model",
                  "image_caption_prompt", "image_mention_window",
                  "image_max_bytes", "switch_method", "log_skipped_messages",
                  "quote_reply_prefix", "quote_reply_native", "mention_as_text",
                  "web_port", "web_host", "astrbot_ob_url", "astrbot_attachments",
                  "astrbot_config_file", "weflow_base_url", "ollama_base_url",
                  "ollama_model", "ollama_timeout", "image_caption_api_base",
                  "weflow_send_api"):
            if k in cur:
                cfg[k] = cur[k]
    cfg["access_token"] = ""
    cfg["bot_wxid"] = ""
    cfg["bot_nicknames"] = ["机器人昵称"]
    io.open(os.path.join(bridge_dir, "config.json"), "w",
            encoding="utf-8", newline="\n").write(
        json.dumps(cfg, ensure_ascii=False, indent=4) + "\n")
    return True


# ───────────────────────── 启动脚本 ─────────────────────────

LAUNCHER = r"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Mon3trBot 启动器

echo ============================================================
echo   Mon3trBot 正在启动
echo ============================================================
echo.

rem ---- 1) 把包内 Python 的绝对路径写回两个 venv 的 pyvenv.cfg ----
rem    Windows 的 venv 靠 pyvenv.cfg 里的 home 找基座解释器；
rem    换台电脑 / 换目录后必须重写，否则 python 起不来。
echo [1/5] 校正运行环境路径...
set "PYDIR=%~dp0python"
if not exist "%PYDIR%\python.exe" (
    echo   [X] 找不到 python\python.exe，压缩包可能没解压完整。
    pause & exit /b 1
)
> "%~dp0astrbot\.venv\pyvenv.cfg" echo home = %PYDIR%
>>"%~dp0astrbot\.venv\pyvenv.cfg" echo include-system-site-packages = false
>>"%~dp0astrbot\.venv\pyvenv.cfg" echo version = 3.13.14
> "%~dp0akasha\.venv\pyvenv.cfg" echo home = %PYDIR%
>>"%~dp0akasha\.venv\pyvenv.cfg" echo include-system-site-packages = false
>>"%~dp0akasha\.venv\pyvenv.cfg" echo version = 3.13.14

rem ---- 2) 依赖检查：微信 + WeFlow ----
echo [2/5] 检查依赖...

rem   微信：必须已安装并登录（发送靠操作微信窗口）
set "WX_FOUND="
for %%D in (
    "%ProgramFiles%\Tencent\Weixin\Weixin.exe"
    "%ProgramFiles%\Tencent\WeChat\WeChat.exe"
    "%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"
) do if exist %%D set "WX_FOUND=1"
if not defined WX_FOUND (
    echo   [!] 没找到已安装的微信。请先安装微信并登录。
    echo       安装包在 installers\ 目录里。
)

rem   WeFlow：优先用已安装的，没有就提示装
tasklist /fi "imagename eq WeFlow.exe" 2>nul | find /i "WeFlow.exe" >nul
if not errorlevel 1 (
    echo   WeFlow 已在运行。
    goto weflow_ok
)
set "WF_EXE="
for %%D in (
    "%LOCALAPPDATA%\Programs\WeFlow\WeFlow.exe"
    "%ProgramFiles%\WeFlow\WeFlow.exe"
) do if exist %%D set "WF_EXE=%%~D"
if defined WF_EXE (
    echo   启动 WeFlow...
    start "" "%WF_EXE%"
    echo   等待 15 秒让它初始化...
    timeout /t 15 /nobreak >nul
) else (
    echo   [!] 没找到已安装的 WeFlow。
    echo       安装包在 installers\ 目录里，装完再跑一次本脚本。
    echo       若你已装好，忽略这条即可。
)
:weflow_ok

rem ---- 3) 启动 AstrBot ----
echo [3/5] 启动 AstrBot（首次约 40~90 秒）...
set "PYTHONPATH="
set "PYTHONUNBUFFERED=1"
set "NO_PROXY=127.0.0.1,localhost,::1"
set "no_proxy=127.0.0.1,localhost,::1"
start "AstrBot" /d "%~dp0astrbot" "%~dp0python\python.exe" -u run_astrbot.py run

echo   等待 AstrBot 就绪...
set /a N=0
:wait_astrbot
timeout /t 3 /nobreak >nul
set /a N+=1
netstat -ano | findstr LISTENING | findstr ":11229" >nul
if not errorlevel 1 goto astrbot_ok
if %N% GEQ 60 goto astrbot_timeout
goto wait_astrbot

:astrbot_timeout
echo   [!] 等了 3 分钟仍未就绪，继续启动桥接（日志见 astrbot\astrbot_run.log）
goto start_bridge

:astrbot_ok
echo   AstrBot 已就绪。

rem ---- 4) 启动桥接 ----
:start_bridge
echo [4/5] 启动桥接...
if exist "%~dp0akasha\bridge.pid" del /q "%~dp0akasha\bridge.pid"
start "Mon3trBot 桥接" /d "%~dp0akasha" "%~dp0python\python.exe" main.py

timeout /t 10 /nobreak >nul
echo.
echo [5/5] 打开控制面板...
start "" "http://127.0.0.1:8766"

echo.
echo ============================================================
echo   已启动完成。
echo     桥接面板   http://127.0.0.1:8766
echo     AstrBot    http://127.0.0.1:6185
echo   两个服务各占一个窗口，关掉窗口即停止。
echo   也可以双击「停止 Mon3trBot.bat」一次全停。
echo ============================================================
echo.
pause
"""

STOPPER = r"""@echo off
chcp 65001 >nul
title 停止 Mon3trBot
echo 正在停止 Mon3trBot...

for %%P in (8766 6185 11229) do (
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr LISTENING ^| findstr ":%%P"') do (
        taskkill /F /PID %%A >nul 2>&1
    )
)
if exist "%~dp0akasha\bridge.pid" del /q "%~dp0akasha\bridge.pid"

echo.
echo AstrBot 与桥接已停止（WeFlow 请自行决定是否关闭）。
echo.
pause
"""

# astrbot/ 目录内的相对路径启动脚本：只拉起 AstrBot 自己，方便单独调试
ASTRBOT_LAUNCHER = r"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title AstrBot

rem 相对定位基座 Python：优先用发行版根目录的 python\，
rem 找不到就回落到本机 PATH 里的 python（这样本目录单独拷走也能跑）。
set "PYEXE=%~dp0..\python\python.exe"
if not exist "%PYEXE%" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [X] 找不到 Python：既没有 ..\python\python.exe，PATH 里也没有 python。
        pause & exit /b 1
    )
    set "PYEXE=python"
)

rem 校正 venv 路径（若用的是包内基座）
if exist "%~dp0..\python\python.exe" (
    > "%~dp0.venv\pyvenv.cfg" echo home = %~dp0..\python
    >>"%~dp0.venv\pyvenv.cfg" echo include-system-site-packages = false
    >>"%~dp0.venv\pyvenv.cfg" echo version = 3.13.14
)

set "PYTHONPATH="
set "PYTHONUNBUFFERED=1"
set "NO_PROXY=127.0.0.1,localhost,::1"
set "no_proxy=127.0.0.1,localhost,::1"

echo 启动 AstrBot（首次约 40~90 秒）...
echo   面板 http://127.0.0.1:6185
echo.
"%PYEXE%" -u run_astrbot.py run
pause
"""


def write_launchers(stage, astrbot_dir):
    # bat 用 GBK 写，cmd 下中文才不乱码
    for name, content, where in (
            ("启动 Mon3trBot.bat", LAUNCHER, stage),
            ("停止 Mon3trBot.bat", STOPPER, stage),
            ("启动 AstrBot.bat", ASTRBOT_LAUNCHER, astrbot_dir)):
        p = os.path.join(where, name)
        io.open(p, "w", encoding="gbk", errors="replace",
                newline="\r\n").write(content)
        log(f"  ✔ {os.path.relpath(p, stage)}")


README_TXT = """# Mon3trBot_Remake · 正式发行版

解压即用。**不需要安装 Python、不需要安装 AstrBot、不需要 pip 装依赖** ——
运行环境都在包里了。

---

## 一、开始之前

| 你需要准备 | 说明 |
|---|---|
| **微信桌面版**（登录） | 机器人靠"操作微信窗口"发消息。安装包在 `installers\\` 里 |
| **WeFlow**（运行并开启 HTTP API） | 负责**读**消息。安装包在 `installers\\` 里 |
| **一个模型 API Key** | 属于个人凭据，**包里不含**，首次配置时填你自己的 |

> 微信窗口启动后请**保持打开**（最小化可以）。完全关掉就发不出消息。

---

## 二、三步跑起来

### 第 1 步：装微信 + 登录
`installers\\` 里有安装包，装完扫码登录。

### 第 2 步：装并配置 WeFlow
1. 用 `installers\\` 里的安装包装好 WeFlow
2. 运行它，按引导完成初始化（它会读本机微信数据）
3. 到设置里确认 **HTTP API 已开启**（端口 `5031`）

### 第 3 步：配置并启动
1. 第一次要先填配置：
   - **桥接**：`akasha\\config.json`（照 `akasha\\config.example.json` 填，
     关键是 WeFlow 的 Access Token）
   - **模型**：打开 AstrBot 面板后在里面配，或改 `astrbot\\data\\cmd_config.json`
2. 双击 **`启动 Mon3trBot.bat`**
   - 自动校正运行环境路径 → 拉起 WeFlow → AstrBot → 桥接
   - 最后自动打开控制面板 <http://127.0.0.1:8766>

停止：双击 **`停止 Mon3trBot.bat`**。

---

## 三、目录说明

| 目录 | 是什么 |
|---|---|
| `akasha\\` | 桥接（微信 ↔ AstrBot）。改配置改这里的 `config.json` |
| `astrbot\\` | AstrBot 本体（模型、人格、知识库、定时任务都归它管） |
| `python\\` | 免安装 Python 基座。**别单独挪走或改名** |
| `installers\\` | WeFlow / 微信 的安装包（装完可以删） |

两个面板：

| 面板 | 地址 |
|---|---|
| 桥接面板 | <http://127.0.0.1:8766> |
| AstrBot 面板 | <http://127.0.0.1:6185> |

---

## 四、常见问题

**Q：启动后没反应？**
看日志：`astrbot\\astrbot_run.log`、`akasha\\bridge_run.log`。
AstrBot 首次启动慢（40~90 秒）属正常。

**Q：消息发不出去？**
1. 微信窗口是不是被完全关掉了？（必须开着）
2. WeFlow 在不在运行？桥接面板上它是不是绿的？
3. 目标会话名对不对？

**Q：机器人不回话？**
多半是模型没配好。进 AstrBot 面板 6185 检查模型是否启用、Key 是否填对。
**换模型后记得设 `max_context_tokens`（建议 65536）。**

**Q：只有某个好友不回，群里却正常？** ⚠️ 最常见的一个坑

平台 ID 白名单（`astrbot\\data\\cmd_config.json` 的 `id_whitelist`）**出厂是空的**，
所以你加完好友后如果开了这个开关，**没勾上的好友发消息会石沉大海**——
桥接日志显示"已推送"，AstrBot 却直接丢掉，看起来像机器人坏了。

正确做法：**用面板勾，不要手写。**
打开 <http://127.0.0.1:8766> →「成员与权限」→「私聊白名单」→ 勾好友 → 保存 →
点旁边的 **「🔄 重启 AstrBot」**。

> 为什么必须用面板：AstrBot 的判定是
> `unified_msg_origin not in whitelist and get_group_id() not in whitelist`。
> **私聊没有 group_id**，所以私聊条目必须写成完整 UMO
> （`wechat_bridge:FriendMessage:<UID>`）。**手写裸数字 UID 在私聊里永远匹配不上**，
> 而且重启也没用——这是格式问题，不是缓存问题。面板会自动帮你写成正确格式。
> 群条目则相反，写裸群号（如 `2000000001`）即可。

**Q：改了白名单/人设，怎么不生效？**
AstrBot 只在**启动时**读一次配置。改完必须重启——面板上点「🔄 重启 AstrBot」
（约 40~90 秒），不用关控制台窗口。

**Q：换台电脑怎么办？**
整个文件夹拷过去，双击 `启动 Mon3trBot.bat` —— 启动脚本会自动把环境路径
改成新位置。但微信和 WeFlow 要在新电脑上重新安装与登录。

---

## 五、关于隐私

这个包里**不含**任何个人数据：

- 无 API Key、无面板口令
- 无聊天记录、无好友/群名单、无定时任务
- 文档与代码里的真实 ID / 群名 / 人名已替换成占位符

保留的是**软件资产**：全部角色人格、普通知识库文档。
构建时对整包跑过脱敏审计，**零残留才会出包**。
"""


# ───────────────────────── 主流程 ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default=DEFAULT_STAGE,
                    help="构建目录（请用短路径，默认项目内 build/）")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--keep-stage", action="store_true",
                    help="打包后保留 stage 目录（默认自动删除）")
    args = ap.parse_args()

    ver = version()
    name = f"{PROJECT_CODE}-v{ver}"
    stage = os.path.join(args.stage, name)
    t00 = time.time()

    log(f"构建 {name}")
    log(f"  构建目录: {stage}")
    log(f"  基座 Python: {base_python_dir()}")

    if os.path.isdir(stage):
        # 上次构建的残留（正常流程会自动删，这里兜失败的构建）。
        # 不再改名成 _old_HHMMSS —— 那会在项目外越攒越多。
        n_stale, b_stale = _walk_stats(stage)
        log(f"  清理上次构建残留：{n_stale} 文件 / {b_stale/1024/1024:.0f} MB")
        rmtree_fast(stage)
        if os.path.isdir(stage):
            raise SystemExit(f"❌ 无法清理残留的构建目录（可能被占用）：{stage}")
    os.makedirs(stage, exist_ok=True)

    py_src = base_python_dir()
    ab_venv_src = os.path.join(RT_ASTRBOT, ".venv")
    br_venv_src = os.path.join(RT_BRIDGE, ".venv")

    check_path_budget(stage, [py_src, ab_venv_src, br_venv_src, CODE_DIR])

    # ---------- 1. 基座 Python ----------
    hr("1/7 复制免安装 Python")
    t0 = time.time()
    n1, b1 = copytree(py_src, os.path.join(stage, "python"),
                      extra_xd=("__pycache__", "test", "tests"))
    log(f"  {n1} 文件 / {b1/1024/1024:.0f} MB / {time.time()-t0:.0f}s")

    # ---------- 2. AstrBot（完整） ----------
    hr("2/7 复制 AstrBot（完整目录，含 .venv / 人格 / 普通知识库）")
    t0 = time.time()
    dst_ab = os.path.join(stage, "astrbot")
    os.makedirs(dst_ab, exist_ok=True)
    for f in ASTRBOT_FILES:
        s = os.path.join(RT_ASTRBOT, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst_ab, f))
    # .venv 整份（AstrBot 框架代码就在 site-packages 里）
    n2a, b2a = copytree(ab_venv_src, os.path.join(dst_ab, ".venv"),
                        raw=True, extra_xd=("__pycache__",))
    log(f"  .venv: {n2a} 文件 / {b2a/1024/1024:.0f} MB")
    # kb_docs（普通知识库文档 —— 保留）
    n2b, _ = copytree(os.path.join(RT_ASTRBOT, "kb_docs"),
                      os.path.join(dst_ab, "kb_docs"))
    log(f"  kb_docs（普通知识库文档，保留）: {n2b} 文件")
    # data：只带必要项
    src_data = os.path.join(RT_ASTRBOT, "data")
    dst_data = os.path.join(dst_ab, "data")
    os.makedirs(dst_data, exist_ok=True)
    if os.path.isfile(os.path.join(src_data, "cmd_config.json")):
        shutil.copy2(os.path.join(src_data, "cmd_config.json"),
                     os.path.join(dst_data, "cmd_config.json"))
    # DB 用 VACUUM INTO 取一致快照（源库可能正被运行中的 AstrBot 使用）
    src_db = os.path.join(src_data, "data_v4.db")
    if os.path.isfile(src_db):
        dst_db = os.path.join(dst_data, "data_v4.db")
        if os.path.isfile(dst_db):
            os.remove(dst_db)
        _c = sqlite3.connect(src_db)
        _c.execute("VACUUM INTO ?", (dst_db,))
        _c.close()
        log("  data_v4.db 已取一致性快照")
    # knowledge_base：只建目录占位，向量文件稍后由 drop_vector_kb 删干净
    for sub in ("knowledge_base", "t2i_templates", "config"):
        s = os.path.join(src_data, sub)
        if os.path.isdir(s):
            nn, bb = copytree(s, os.path.join(dst_data, sub))
            log(f"  data/{sub}: {nn} 文件 / {bb/1024/1024:.1f} MB")
    os.makedirs(os.path.join(dst_data, "attachments"), exist_ok=True)
    log(f"  用时 {time.time()-t0:.0f}s")

    # ---------- 3. 桥接（完整） ----------
    hr("3/7 复制 Mon3trBot 桥接（完整运行副本）")
    t0 = time.time()
    dst_br = os.path.join(stage, "akasha")
    os.makedirs(dst_br, exist_ok=True)
    n3a, b3a = copytree(br_venv_src, os.path.join(dst_br, ".venv"),
                        raw=True, extra_xd=("__pycache__",))
    log(f"  .venv: {n3a} 文件 / {b3a/1024/1024:.0f} MB")
    for f in BRIDGE_FILES:
        s = os.path.join(CODE_DIR, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dst_br, f))
    log(f"  源码 {len(BRIDGE_FILES)} 个文件")
    log("  （桥接 data/ 不带：里面有真实 wxid / 群名 / 人名）")
    log(f"  用时 {time.time()-t0:.0f}s")

    # ---------- 4. 安装包 ----------
    hr("4/7 收录安装包（WeFlow / 微信）")
    found = find_installers()
    for k in ("WeFlow", "微信"):
        log(f"  {k}: {found.get(k) or '❌ 未找到（发行版内会写明需自行安装）'}")
    inst_lines = copy_installers(os.path.join(stage, "installers"), found)

    # ---------- 5. 脱敏 ----------
    hr("5/7 脱敏（配置 / 数据库 / 向量知识库）")
    st = sanitize_cmd_config(dst_data)
    log(f"  cmd_config.json 剥离 {len(st)} 项密钥/真实 ID")
    cleared, n_persona = sanitize_db(dst_data)
    log(f"  data_v4.db 清空 {len(cleared)} 张表；保留 personas 表 {n_persona} 条人格")
    if n_persona == 0:
        log("  ⚠️ 警告：personas 表为空！角色人格是软件资产，不该为空。")
    elif n_persona < 4:
        # 人格是软件资产，少了就是被误删（2026-09-18：moatima 加入后共 4 个）
        log(f"  ⚠️ 警告：人格只有 {n_persona} 条，少于预期的 4 条，请确认是否误删。")
    vec = drop_vector_kb(dst_data)
    log(f"  向量知识库已删除 {len(vec)} 个文件"
        + (f": {', '.join(vec[:6])}{' …' if len(vec) > 6 else ''}" if vec else ""))
    if sanitize_bridge_config(dst_br):
        log("  桥接 config.json 已按模板重建（token/wxid/昵称已清空）")
    # venv 里写死的本机绝对路径 / 用户名（pyvenv.cfg、activate*）
    nv = neutralize_venv_paths(stage)
    log(f"  venv 本机路径已清理：{len(nv)} 个文件")

    # 启动脚本与说明（放在脱敏之后，确保它们本身也被审计扫到）
    write_launchers(stage, dst_ab)
    io.open(os.path.join(stage, "使用说明.md"), "w",
            encoding="utf-8", newline="\n").write(README_TXT)
    log("  ✔ 使用说明.md")

    import sanitize_privacy as sp
    ids = sp.collect_identifiers()
    literal, bounded_raw = sp.build_map(ids)
    bounded = sp.compile_bounded(bounded_raw)
    log("  正在对整包做文本脱敏（含文档 / 脚本 / 本机路径）...")
    sp.apply_map(stage, literal, bounded)

    # ---------- 6. 校验 ----------
    hr("6/7 校验：整包必须零隐私残留")
    bad = sp.audit(stage, literal, bounded)
    # 额外硬检查：本机用户名 / 绝对路径
    hard_hits = []
    for base, dirs, fs in os.walk(stage):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in fs:
            if f.lower().endswith((".pyc", ".pyd", ".dll", ".exe", ".pak",
                                   ".bin", ".faiss", ".db", ".zip", ".node",
                                   ".asar", ".png", ".jpg", ".ico", ".ttf")):
                continue
            p = os.path.join(base, f)
            try:
                if os.path.getsize(p) > 4 * 1024 * 1024:
                    continue
                s = io.open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for h in HARD_LITERALS:
                if h in s:
                    hard_hits.append((os.path.relpath(p, stage), h))
    if hard_hits:
        log(f"  ❌ 硬检查命中 {len(hard_hits)} 处本机标识：")
        for rel, h in hard_hits[:15]:
            log(f"      {rel}  ->  {h!r}")
    else:
        log("  ✅ 硬检查通过（无本机用户名 / 绝对路径）")

    # 媒体指纹兜底：md5 / aeskey / cdn url / emojiinfo
    media_hits = scan_media_fingerprints(stage)
    if media_hits:
        log(f"  ❌ 媒体指纹兜底命中 {len(media_hits)} 处：")
        for rel, frag in media_hits[:15]:
            log(f"      {rel}  ->  {frag}")
    else:
        log("  ✅ 媒体指纹兜底通过（无真实 md5 / aeskey / cdn / emojiinfo）")

    # 密钥兜底扫描：不看字段名、只看内容特征
    secret_hits = scan_for_secrets(stage)
    if secret_hits:
        log(f"  ❌ 密钥兜底扫描命中 {len(secret_hits)} 处：")
        for rel, frag in secret_hits[:15]:
            log(f"      {rel}  ->  {frag}")
    else:
        log("  ✅ 密钥兜底扫描通过（无 sk- / nvapi- / Bearer 等真实密钥）")

    prune_empty(stage)
    if bad or hard_hits or secret_hits or media_hits:
        log(f"\n❌ 仍有隐私残留（audit {bad} 文件 / 硬检查 {len(hard_hits)} 处 / "
            f"密钥 {len(secret_hits)} 处 / 媒体指纹 {len(media_hits)} 处），"
            f"已中止出包。")
        return 1

    # ---------- BUILD_INFO（必须写在打包**之前**，否则不进 zip）----------
    lines = [
        f"# {name}", "",
        f"- 构建时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 源码版本：v{ver}",
        "", "## 安装包", "",
        *inst_lines, "",
        "## 内含", "",
        "- `akasha/` 桥接完整运行副本（含 .venv）",
        "- `astrbot/` AstrBot 完整副本（含 .venv / 全部角色人格 / 普通知识库文档）",
        "- `python/` 免安装 Python 基座",
        "", "## 隐私", "",
        "- 已剥离：全部 API Key / 面板口令 / jwt_secret",
        "- 已清空：聊天历史、会话映射、定时任务、好友与群名单",
        "- 已删除：向量知识库索引（doc.db / index.faiss）",
        "- 已保留：三个角色人格、普通知识库文档（软件资产）",
        "- 构建时对整包跑过脱敏审计 + 本机标识硬检查 + 密钥兜底扫描，零残留才会出包",
    ]
    io.open(os.path.join(stage, "BUILD_INFO.md"), "w",
            encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    log("  ✔ BUILD_INFO.md")

    # ---------- 7. 打包 ----------
    zip_path = None
    if not args.no_zip:
        hr("7/7 打包 zip")
        os.makedirs(RELEASE_DIST, exist_ok=True)
        zip_path = os.path.join(RELEASE_DIST, name + ".zip")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        t0 = time.time()
        total = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for base, dirs, fs in os.walk(stage):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in fs:
                    full = os.path.join(base, f)
                    arc = os.path.join(name, os.path.relpath(full, stage))
                    z.write(full, arc)
                    total += 1
        sz = os.path.getsize(zip_path) / 1024 / 1024
        log(f"  {total} 文件 / {sz:.0f} MB / {time.time()-t0:.0f}s")
        log(f"  {zip_path}")

    # ---------- stage 清理 ----------
    # zip 已经出来了，stage 只是中间产物：删掉，免得四处攒解压副本。
    # --no-zip / --keep-stage 时保留，便于排查（如检查脱敏后的实际内容）。
    if zip_path and not args.keep_stage:
        hr("清理 stage（zip 已生成）")
        n_s, b_s = _walk_stats(stage)
        rmtree_fast(stage)
        if os.path.isdir(stage):
            log(f"  ⚠️ stage 未能完全删除，请手动清理：{stage}")
        else:
            log(f"  已删除 {stage}（{n_s} 文件 / {b_s/1024/1024:.0f} MB）")
    elif not args.no_zip:
        log(f"  保留 stage（--keep-stage）：{stage}")

    log(f"\n✅ 发行版构建完成（总耗时 {time.time()-t00:.0f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
