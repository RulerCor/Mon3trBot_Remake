# -*- coding: utf-8 -*-
"""独立图片消息（非引用）回归测试（2026-10-30）。

背景：Alpha 的 SSE `message.new` 只给 10 个字段
（accountId/avatarUrl/content/event/eventId/groupName/rawid/sessionId/
sessionType/sourceName/timestamp），**不含** refs / source_type / media。
独立图片消息的 content 为空 → 旧逻辑直接判「空内容」整条丢弃，bot
永远看不到群友发的图（bridge.log 里成片的 `跳过 …: 空内容`）。

本测试覆盖：
  1. 归一化层：只有 refs[].kind=image / mime_type 时能认出图片；
  2. 映射层：alpha-media://sha256/<h> → /api/v1/media/<h>；
  3. 投递层：空内容事件经后台回查补全成 [图片] 后入缓冲（mock 历史，不联网）；
  4. 降级层：查不到 / 真的是空 → 仍然跳过，不注入幻觉内容。

用桥接 venv 跑：
  runtime/bridge/.venv/Scripts/python.exe scripts/test_standalone_image.py
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "wechat-weflow-bridge-ob11")
sys.path.insert(0, SRC)

import message_source as MS        # noqa: E402
import bridge_core as BC           # noqa: E402
import config                      # noqa: E402

fail = 0


def check(name, ok, detail=""):
    global fail
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        fail += 1


# 与线上实测一致的 SSE 事件形状：只有这 10 个字段，没有媒体信息。
SSE_IMG_EVENT = {
    "event": "message.new",
    "eventId": "message.new:123@chatroom:9001",
    "accountId": "acct",
    "sessionId": "123@chatroom",
    "sessionType": "group",
    "rawid": "9001",
    "avatarUrl": "https://x/y.jpg",
    "sourceName": "wxid_sender",
    "groupName": "某群",
    "content": "",
    "timestamp": 1793300000,
}

# 历史接口里同一条消息（snake_case，只有 refs + mediaUrl）。
HIST_IMG_MSG = {
    "conversation_id": "123@chatroom",
    "kind": "media",
    "source_type": 3,
    "server_id": "9001",
    "platform_message_id": "9001",
    "sender_id": "wxid_sender",
    "sender_name": "群友A",
    "sent_at_unix_ms": 1793300000000,
    "text": "",
    "mediaUrl": "/api/v1/media/abc123",
    "refs": [{"id": "abc123", "kind": "image",
              "mime_type": "image/jpeg",
              "uri": "alpha-media://sha256/abc123"}],
    "media": {"resources": [{"mimeType": "image/jpeg", "status": "resolved",
                             "url": "/api/v1/media/abc123"}]},
}

# 只有 refs 的形态（无 mediaUrl / media）：必须靠 alpha-media:// 映射取 URL。
HIST_REFS_ONLY = {
    "conversation_id": "123@chatroom",
    "server_id": "9002",
    "mime_type": "image/png",
    "refs": [{"id": "def456", "kind": "image", "mime_type": "image/png",
              "uri": "alpha-media://sha256/def456"}],
}


class FakeSource:
    """替身：返回固定历史，记录查询次数。"""
    name = "alpha"

    def __init__(self, messages):
        self.messages = messages
        self.calls = 0

    def fetch_messages(self, talker, **kw):
        self.calls += 1
        return {"messages": self.messages}

    def download_media(self, url):
        raise AssertionError("本测试不应真的下载")

    def normalize_event(self, data):
        # 用真实 AlphaSource 做归一（FakeSource 只管历史返回，不重实现解析）。
        return MS.AlphaSource().normalize_event(data)


def test_infer():
    print("\n[1] 归一化层：只靠 refs/kind/mime 认出图片")
    check("refs 图片 → type=3", MS.infer_event_type(HIST_REFS_ONLY) == 3,
          MS.infer_event_type(HIST_REFS_ONLY))
    check("media.resources 图片 → type=3",
          MS.infer_event_type(HIST_IMG_MSG) == 3)
    check("无任何媒体线索 → type=0", MS.infer_event_type(SSE_IMG_EVENT) == 0)
    # 独立图片：SSE 事件本身没有线索，但历史能读出来 → 归一后是 [图片]
    norm = MS.AlphaSource().normalize_event(dict(HIST_REFS_ONLY))
    check("历史归一 → [图片]", norm.get("content") == "[图片]", norm.get("content"))
    check("历史归一 → 媒体 URL",
          norm.get("_alphaMediaUrl") == "/api/v1/media/def456",
          norm.get("_alphaMediaUrl"))


def test_uri_map():
    print("\n[2] 映射层：alpha-media:// → /api/v1/media/")
    check("sha256 前缀映射",
          MS.alpha_media_uri_to_url("alpha-media://sha256/abc") == "/api/v1/media/abc",
          MS.alpha_media_uri_to_url("alpha-media://sha256/abc"))
    check("无 sha256 前缀也能映射",
          MS.alpha_media_uri_to_url("alpha-media://abc") == "/api/v1/media/abc")
    check("空/非本协议 → 空串",
          MS.alpha_media_uri_to_url("") == ""
          and MS.alpha_media_uri_to_url("https://x/y") == "")
    check("extract_media_url 读 refs[].uri",
          MS.extract_media_url(HIST_REFS_ONLY) == "/api/v1/media/def456",
          MS.extract_media_url(HIST_REFS_ONLY))


class FakeBridge(BC.WeFlowBridge):
    """绕过真 __init__（不需要 sender），只补回查用到的状态。"""
    def __init__(self):
        self.processed_ids = set()
        self.pending_buffers = {}
        self.buffer_lock = __import__("threading").Lock()
        self._skip_stat = {}
        self._skip_lock = __import__("threading").Lock()
        self._empty_lookup_sem = __import__("threading").Semaphore(4)


def test_delivery():
    print("\n[3] 投递层：空内容事件回查补全 → 入缓冲")
    bridge = FakeBridge()
    fake = FakeSource([HIST_IMG_MSG])
    BC.create_message_source = lambda: fake

    enriched = bridge._hydrate_empty_event(dict(SSE_IMG_EVENT))
    check("回查成功返回补全事件", enriched is not None)
    check("补全后是 [图片]", enriched and enriched.get("content") == "[图片]",
          enriched and enriched.get("content"))
    check("补全后带媒体 URL",
          enriched and enriched.get("_alphaMediaUrl") == "/api/v1/media/abc123",
          enriched and enriched.get("_alphaMediaUrl"))
    check("保留 SSE 的 rawid 用于去重",
          enriched and enriched.get("rawid") == "9001")
    check("保留 SSE 的群名",
          enriched and enriched.get("groupName") == "某群")
    check("合并后不再被判空内容",
          enriched and bridge.ignore_reason(enriched) is None,
          enriched and bridge.ignore_reason(enriched))


def test_delivery_async_into_buffer():
    print("\n[3b] 异步路径：worker 写完缓冲后可读到 [图片]")
    bridge = FakeBridge()
    fake = FakeSource([HIST_IMG_MSG])
    BC.create_message_source = lambda: fake

    bridge._hydrate_empty_event_async(dict(SSE_IMG_EVENT))
    deadline = time.time() + 8
    imgs = []
    while time.time() < deadline:
        with bridge.buffer_lock:
            for entry in bridge.pending_buffers.values():
                imgs.extend(entry.get("pending_images", []))
        if imgs:
            break
        time.sleep(0.05)
    # 图片走 pending_images 分支（下载后作为 image 段投递），不是文本。
    check("缓冲里出现待发图片", bool(imgs), str(imgs))
    check("待发图片内容已归一为 [图片]",
          any(d.get("content") == "[图片]" for d in imgs), str(imgs))
    check("独立图片不再被跳过", bool(imgs))


def test_degrade():
    print("\n[4] 降级层：查不到 / 真空内容 → 照旧跳过")
    bridge = FakeBridge()
    empties = [dict(SSE_IMG_EVENT, rawid="9999")]

    class NoMatch(FakeSource):
        def fetch_messages(self, talker, **kw):
            self.calls += 1
            return {"messages": []}

    BC.create_message_source = lambda: NoMatch([])
    check("历史查不到 → 返回 None",
          bridge._hydrate_empty_event(dict(SSE_IMG_EVENT)) is None)

    # 无 rawid：不启动回查，直接跳过
    bridge2 = FakeBridge()
    BC.create_message_source = lambda: FakeSource([HIST_IMG_MSG])
    e = dict(SSE_IMG_EVENT)
    e.pop("rawid")
    bridge2._hydrate_empty_event_async(e)
    time.sleep(0.3)
    with bridge2.buffer_lock:
        total = sum(len(x.get("messages", [])) for x in bridge2.pending_buffers.values())
    check("无 rawid 不注入任何内容", total == 0, str(total))

    # 历史里是纯文本（不该被这里注入）
    class TextOnly(FakeSource):
        pass
    txt = dict(HIST_IMG_MSG)
    txt["refs"] = []
    txt["media"] = {}
    txt["mediaUrl"] = None
    txt["kind"] = "text"
    txt["source_type"] = 1
    txt["text"] = "hello"
    BC.create_message_source = lambda: TextOnly([txt])
    check("非媒体历史不补全成文本",
          bridge._hydrate_empty_event(dict(SSE_IMG_EVENT)) is None)


def main():
    print("=" * 62)
    print("独立图片消息（非引用）回归测试")
    print("=" * 62)
    test_infer()
    test_uri_map()
    test_delivery()
    test_delivery_async_into_buffer()
    test_degrade()
    print("\n" + "=" * 62)
    print(f"结果: {'✅ 全部通过' if fail == 0 else f'❌ {fail} 项失败'}")
    print("=" * 62)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
