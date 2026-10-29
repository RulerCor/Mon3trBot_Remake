# -*- coding: utf-8 -*-
"""引用图片投递回归测试（2026-10-29）。

背景：微信引用一张图片时，正文只有「〔引用 某某：[图片]〕」，桥接旧逻辑
只判断 <img 就写死 [图片]，把 md5/svrid 全丢掉 → AstrBot 收不到 image
段 → 不触发识图 → 模型只能答"看不清"。

本测试覆盖三层：
  1. 解析层：从真实引用 XML 解出 quote_image 元数据，text 不降级
  2. 投递层：桥接把引用图片下载成 image 段（mock 下载，不联网）
  3. 降级层：下载失败/没有 svrid 时，[图片] 文本照常推送（能力不缩水）

用桥接 venv 跑：
  runtime/bridge/.venv/Scripts/python.exe scripts/test_quote_image.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "wechat-weflow-bridge-ob11")
sys.path.insert(0, SRC)

import wx_msg_parser as P          # noqa: E402
import bridge_core as BC           # noqa: E402
import config                      # noqa: E402

SAMPLE = os.path.join(ROOT, "wechat-weflow-bridge-ob11",
                      "tests", "fixtures", "quote_image.xml")

# 1x1 透明 PNG（假下载结果，只为验证段结构）
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8AAAwAB/wFPh5JzAAAAAElFTkSuQmCC")

fail = 0


def check(name, ok, detail=""):
    global fail
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fail += 1


def test_parse():
    print("\n[1] 解析层")
    raw = io.open(SAMPLE, encoding="utf-8").read()
    p = P.parse_content(raw, bot_nicknames=("Mon3tr", "M3"))
    qi = (p.extra or {}).get("quote_image") or {}
    check("kind=quote", p.kind == P.KIND_QUOTE, p.kind)
    check("text 仍是 [图片]", "[图片]" in p.text, p.text)
    check("text 不含内部 XML", "<img" not in p.text and "aeskey" not in p.text)
    check("svrid 已解出", qi.get("svrid") == "7066182232866673373", qi.get("svrid"))
    check("md5 已解出", qi.get("md5") == "3398762cea6e4388c6788b2ec2f2ba20", qi.get("md5"))
    check("size 已解出", qi.get("size") == 87998, qi.get("size"))
    check("url_kind 已解出", qi.get("url_kind") == "cdnbigimgurl", qi.get("url_kind"))
    return p, raw


def make_bridge(fake_b64):
    bridge = BC.WeFlowBridge(sender=object())
    calls = []

    def fake_fetch(data, max_pages=1):
        calls.append((data, max_pages))
        return fake_b64

    bridge._fetch_image_base64 = fake_fetch
    return bridge, calls


def handle(bridge, parsed, data):
    bridge._handle_rich_message(
        parsed, data,
        is_group=False, session_id_data="12345678901@chatroom",
        sender_in_group="", group_name_raw="测试群",
        source_name="群友A", mentioned=True,
    )


def drain_quote_segments(bridge):
    """取到 pending_quote_images 里并发持有器的最终 segment（等它完成）。"""
    segs = []
    for entry in bridge.pending_buffers.values():
        for holder in entry.get("pending_quote_images", []):
            ev = holder.get("event") if isinstance(holder, dict) else None
            if ev is not None:
                ev.wait(timeout=10)
            seg = holder.get("segment") if isinstance(holder, dict) else None
            if seg:
                segs.append(seg)
    return segs


def test_delivery(parsed, raw):
    print("\n[2] 投递层（下载成功 → 生成 image 段）")
    bridge, calls = make_bridge(PNG_B64)
    handle(bridge, parsed, {"content": raw, "sessionId": "12345678901@chatroom",
                            "sourceName": "群友A"})
    segs = drain_quote_segments(bridge)
    check("生成了 image 段", len(segs) == 1, str(len(segs)))
    check("段类型=image", segs and segs[0].get("type") == "image")
    check("段数据是 base64://",
          segs and str(segs[0].get("data", {}).get("file", "")).startswith("base64://"))
    check("回查用了 svrid",
          calls and calls[0][0].get("rawid") == "7066182232866673373",
          str(calls[0][0]) if calls else "no call")
    # SSE 接收线程必须立刻返回（回查在网络线程里做）
    check("回查不在接收线程同步做（并发启动）",
          any(e.get("pending_quote_images") for e in bridge.pending_buffers.values()))
    # 文本必须同步进缓冲区（不能因为加图丢了文本）
    texts = []
    for entry in bridge.pending_buffers.values():
        texts.extend(entry.get("messages", []))
    check("文本同步入缓冲区", any("[图片]" in t for t in texts), str(texts))


def test_degrade(parsed, raw):
    print("\n[3] 降级层（下载失败 / 缺 svrid → 文本照常推送）")
    # 3a. 下载返回 None
    bridge, calls = make_bridge(None)
    handle(bridge, parsed, {"content": raw, "sessionId": "12345678901@chatroom",
                            "sourceName": "群友A"})
    segs = drain_quote_segments(bridge)
    texts = []
    for entry in bridge.pending_buffers.values():
        texts.extend(entry.get("messages", []))
    check("下载失败不加 image 段", not segs, str(segs))
    check("下载失败文本仍在", any("[图片]" in t for t in texts), str(texts))

    # 3b. 解析不出 svrid（比如引用的是文本）
    p2 = P.parse_content(
        '<?xml version="1.0"?><msg><appmsg><title>看这个</title><type>57</type>'
        '<refermsg><content>某人: 你好</content><displayname>某人</displayname>'
        '<svrid>111</svrid><type>1</type></refermsg></appmsg></msg>')
    bridge2, calls2 = make_bridge(PNG_B64)
    handle(bridge2, p2, {"content": "x", "sessionId": "s", "sourceName": "某人"})
    segs2 = drain_quote_segments(bridge2)
    check("文本引用不触发取图", not segs2 and not calls2, str(calls2))
    check("文本引用文本保留", "你好" in p2.text, p2.text)


def main():
    print("=" * 62)
    print("引用图片投递回归测试")
    print("=" * 62)
    parsed, raw = test_parse()
    test_delivery(parsed, raw)
    test_degrade(parsed, raw)
    print("\n" + "=" * 62)
    print(f"结果: {'✅ 全部通过' if fail == 0 else f'❌ {fail} 项失败'}")
    print("=" * 62)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
