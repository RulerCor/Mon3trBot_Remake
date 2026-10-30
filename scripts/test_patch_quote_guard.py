# -*- coding: utf-8 -*-
"""Isolation tests for the quoted-image caption guard patch.

Run with the AstrBot virtualenv:
  runtime/astrbot/.venv/Scripts/python.exe scripts/test_patch_quote_guard.py

The test imports the patch plugin without starting AstrBot, installs its quote
guard over the real upstream function, and uses a real Provider subclass. This
specifically protects the two contracts that broke in production:
  * the object returned by the plugin context must remain a Provider;
  * the provider's text_chat must be restored after success/failure.
"""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import io
import os
import sys
import time
from types import SimpleNamespace

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_MAIN = os.path.join(ROOT, "scripts", "akasha_rc_patches_plugin", "main.py")

from astrbot.core import astr_main_agent as AM  # noqa: E402
from astrbot.core.agent.message import TextPart  # noqa: E402
from astrbot.core.message.components import Reply  # noqa: E402
from astrbot.core.provider.entities import ProviderRequest  # noqa: E402
from astrbot.core.provider.provider import Provider  # noqa: E402

FAIL = 0
PASS = 0


def check(name, ok, detail=""):
    global FAIL, PASS
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def load_plugin():
    spec = importlib.util.spec_from_file_location("akasha_rc_patches_quote_test", PLUGIN_MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProvider(Provider):
    def __init__(self, behavior="success"):
        super().__init__(
            {"type": "openai_chat_completion", "id": "fake-caption-provider"},
            {},
        )
        self.behavior = behavior
        self.calls = 0
        self.isinstance_seen = False
        self.prompt_seen = None
        self.image_urls_seen = None

    def get_current_key(self):
        return "test-key"

    def set_key(self, key):
        pass

    async def get_models(self):
        return ["fake-vision"]

    async def text_chat(self, *args, **kwargs):
        self.calls += 1
        self.isinstance_seen = isinstance(self, Provider)
        self.prompt_seen = kwargs.get("prompt")
        self.image_urls_seen = kwargs.get("image_urls")
        if self.behavior == "sleep":
            await asyncio.sleep(0.6)
        if self.behavior == "raise":
            raise RuntimeError("synthetic provider failure")
        text = "" if self.behavior == "empty" else "一只橘猫趴在窗台上"
        return SimpleNamespace(completion_text=text)


class FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider

    async def get_using_provider_async(self, umo):
        return self.provider


def make_event():
    quote = Reply(
        id="quote-1",
        sender_nickname="测试用户",
        message_str="被引用的原话",
        chain=[],
    )
    return SimpleNamespace(
        message_obj=SimpleNamespace(message=[quote]),
        unified_msg_origin="test:GroupMessage:123",
    )


def make_req():
    return ProviderRequest(prompt="图里是什么？", extra_user_content_parts=[])


def texts(req):
    return [str(getattr(part, "text", "") or "") for part in req.extra_user_content_parts]


def install_guard(plugin):
    result = plugin._patch_main_agent_quote_caption_guard()
    return result


async def run_caption(plugin, upstream, provider, behavior="success"):
    provider.behavior = behavior
    install_guard(plugin)
    req = make_req()
    before = provider.text_chat
    had_instance = "text_chat" in provider.__dict__
    started = time.monotonic()
    result = await AM._process_quote_message(
        make_event(), req, "fake-caption-provider", FakeContext(provider),
        image_ref="local://quoted-cat.png",
    )
    elapsed = time.monotonic() - started
    AM._process_quote_message = upstream
    return result, req, before, had_instance, elapsed


async def test_success_and_identity(plugin, upstream):
    print("\n[1] 成功路径：真实 Provider 身份 + 原结果 + 原位恢复")
    provider = FakeProvider("success")
    result, req, before, had_instance, elapsed = await run_caption(
        plugin, upstream, provider, "success"
    )
    check("返回 captioned_ref", result == "local://quoted-cat.png", repr(result))
    check("provider 仍是真实 Provider 子类", provider.isinstance_seen)
    check("text_chat 实际被调用", provider.calls == 1, str(provider.calls))
    check("图片 URL 传给识图 provider", provider.image_urls_seen == ["local://quoted-cat.png"])
    check("引用文字保留", any("被引用的原话" in t for t in texts(req)), repr(texts(req)))
    check("识别描述保留", any("一只橘猫" in t for t in texts(req)), repr(texts(req)))
    check("成功路径不泄漏实例级 text_chat", not had_instance and "text_chat" not in provider.__dict__)
    check("恢复为原绑定方法", getattr(provider.text_chat, "__func__", None) is getattr(before, "__func__", None))
    print(f"        成功路径耗时 {elapsed * 1000:.0f}ms")


async def test_empty_and_exception(plugin, upstream):
    print("\n[2] 空描述 / provider 异常：显式失败标记且正文不丢")
    for behavior, label in (("empty", "空描述"), ("raise", "provider 异常")):
        provider = FakeProvider(behavior)
        result, req, before, had_instance, _ = await run_caption(
            plugin, upstream, provider, behavior
        )
        joined = "\n".join(texts(req))
        check(f"{label}: 返回 None", result is None, repr(result))
        check(f"{label}: 有失败标记", "[Quoted Image Caption Unavailable]" in joined, joined)
        check(f"{label}: 引用文字仍在", "被引用的原话" in joined, joined)
        check(f"{label}: 不泄漏实例级 text_chat", not had_instance and "text_chat" not in provider.__dict__)


async def test_timeout(plugin, upstream):
    print("\n[3] 超时：限定预算内返回，不拖整轮")
    provider = FakeProvider("sleep")
    old_cache = dict(plugin._CAPTION_TIMEOUT_CACHE)
    plugin._CAPTION_TIMEOUT_CACHE.clear()
    plugin._CAPTION_TIMEOUT_CACHE["value"] = (time.monotonic() + 60.0, 0.15)
    try:
        result, req, before, had_instance, elapsed = await run_caption(
            plugin, upstream, provider, "sleep"
        )
    finally:
        plugin._CAPTION_TIMEOUT_CACHE.clear()
        plugin._CAPTION_TIMEOUT_CACHE.update(old_cache)
    joined = "\n".join(texts(req))
    check("超时返回 None", result is None, repr(result))
    check("超时预算按配置生效", elapsed < 0.55, f"{elapsed:.3f}s")
    check("超时有失败标记", "[Quoted Image Caption Unavailable]" in joined, joined)
    check("超时引用文字仍在", "被引用的原话" in joined, joined)
    check("超时不泄漏实例级 text_chat", not had_instance and "text_chat" not in provider.__dict__)


async def test_astrna_wrapper_chain(plugin, upstream):
    print("\n[4] AstrNa provider wrapper 链：串联调用后恢复原 wrapper")
    provider = FakeProvider("success")
    base = provider.text_chat
    prompt_context = []

    async def astrna_wrapper(*args, **kwargs):
        prompt_context.append(kwargs.get("prompt"))
        return await base(*args, **kwargs)

    # AstrNa installs a function as an instance attribute, so restoration must
    # restore that exact object rather than an arbitrary bound method.
    provider.text_chat = astrna_wrapper
    install_guard(plugin)
    req = make_req()
    result = await AM._process_quote_message(
        make_event(), req, "fake-caption-provider", FakeContext(provider),
        image_ref="local://quoted-cat.png",
    )
    AM._process_quote_message = upstream
    check("AstrNa wrapper 仍被调用", len(prompt_context) == 1, str(prompt_context))
    check("AstrNa 链上仍成功", result == "local://quoted-cat.png", repr(result))
    check("恢复原 AstrNa wrapper 对象", provider.text_chat is astrna_wrapper)
    check("AstrNa wrapper 后没有残留 bounded wrapper", provider.text_chat is not getattr(plugin, "_never", None))


async def test_wrapped_quote_function(plugin, upstream):
    print("\n[5] 引用函数被 functools.wraps 包装后：不重复套 guard")
    calls = []

    @functools.wraps(upstream)
    async def astrna_quote_wrapper(*args, **kwargs):
        calls.append(1)
        return await upstream(*args, **kwargs)

    AM._process_quote_message = astrna_quote_wrapper
    provider = FakeProvider("success")
    result = None
    try:
        result = install_guard(plugin)
        check("包装后的上游函数可安装", result is not None, repr(result))
        check("再次安装被 marker 检测拦截", install_guard(plugin) is None)
        req = make_req()
        cap = await AM._process_quote_message(
            make_event(), req, "fake-caption-provider", FakeContext(provider),
            image_ref="local://quoted-cat.png",
        )
        check("包装链正常返回", cap == "local://quoted-cat.png", repr(cap))
        check("包装链只进入一次", calls == [1], repr(calls))
    finally:
        AM._process_quote_message = upstream


def test_marker_walker(plugin):
    print("\n[6] marker walker：__wrapped__ / partial / closure")
    marker = "_akasha_quote_caption_guard"

    async def guarded(*args, **kwargs):
        return None

    setattr(guarded, marker, True)

    @functools.wraps(guarded)
    async def decorated(*args, **kwargs):
        return await guarded(*args, **kwargs)

    # Remove the copied attribute so the walker must follow __wrapped__.
    if hasattr(decorated, marker):
        delattr(decorated, marker)

    async def closure_wrapper(*args, **kwargs):
        return await guarded(*args, **kwargs)

    check("直接 marker", plugin._akasha_callable_contains_marker(guarded, marker))
    check("__wrapped__", plugin._akasha_callable_contains_marker(decorated, marker))
    check("functools.partial", plugin._akasha_callable_contains_marker(
        functools.partial(decorated), marker
    ))
    check("闭包捕获", plugin._akasha_callable_contains_marker(closure_wrapper, marker))


async def test_astrna_montage_compat(plugin):
    print("\n[7] AstrNa 五参数 wrapper：montage_refs 不报错且保留动画提示")
    old_notice = getattr(AM, "ANIMATION_CAPTION_NOTICE", None)
    old_ensure = AM._ensure_img_caption
    AM.ANIMATION_CAPTION_NOTICE = "ANIMATION NOTICE {indices}"
    captured = {}

    async def astrna_five_arg(event, req, cfg, plugin_context, provider_id):
        captured["cfg"] = cfg
        return {"ok": True}

    astrna_five_arg._astrna_image_caption_patch = True
    AM._ensure_img_caption = astrna_five_arg
    try:
        first = plugin._patch_astrna_montage_compat()
        check("montage 兼容层可安装", first is not None, repr(first))
        check("重复安装幂等", plugin._patch_astrna_montage_compat() is None)
        req = make_req()
        req.image_urls = ["frame-1.jpg", "frame-2.jpg"]
        cfg = {"image_caption_prompt": "BASE "}
        result = await AM._ensure_img_caption(
            SimpleNamespace(), req, cfg, FakeContext(None), "vision",
            montage_refs={"frame-2.jpg"},
        )
        prompt = captured["cfg"]["image_caption_prompt"]
        check("五参数 AstrNa wrapper 正常返回", result == {"ok": True}, repr(result))
        check("动画提示进入 caption prompt", prompt == "BASE ANIMATION NOTICE 2", repr(prompt))
        check("原 cfg 不被修改", cfg == {"image_caption_prompt": "BASE "}, repr(cfg))
        req.image_urls = ["plain.jpg"]
        await AM._ensure_img_caption(
            SimpleNamespace(), req, cfg, FakeContext(None), "vision",
            montage_refs=set(),
        )
        check("非动画图片不加提示", captured["cfg"] is cfg, repr(captured["cfg"]))
    finally:
        AM._ensure_img_caption = old_ensure
        if old_notice is None:
            delattr(AM, "ANIMATION_CAPTION_NOTICE")
        else:
            AM.ANIMATION_CAPTION_NOTICE = old_notice


async def test_private_companion_lifecycle_repair(plugin):
    print("\n[8] Private Companion 生命周期钩子：未绑定函数修复且幂等")
    from astrbot.core.star.star import StarMetadata, star_map
    from astrbot.core.star.star_handler import (
        EventType, StarHandlerMetadata, star_handlers_registry,
    )

    module_path = "astrbot_plugin_private_companion.main"
    old_meta = star_map.get(module_path)
    old_handlers = list(star_handlers_registry)
    calls = []

    class FakePrivateCompanion:
        async def _on_external_plugin_loaded(self, metadata, *args, **kwargs):
            calls.append(("loaded", metadata))

        async def _on_external_plugin_unloaded(self, metadata, *args, **kwargs):
            calls.append(("unloaded", metadata))

    instance = FakePrivateCompanion()
    star_map[module_path] = StarMetadata(
        name="astrbot_plugin_private_companion",
        module_path=module_path,
        star_cls_type=FakePrivateCompanion,
        star_cls=instance,
    )
    try:
        for event_type, name in (
            (EventType.OnPluginLoadedEvent, "_on_external_plugin_loaded"),
            (EventType.OnPluginUnloadedEvent, "_on_external_plugin_unloaded"),
        ):
            raw = getattr(FakePrivateCompanion, name)
            md = StarHandlerMetadata(
                event_type=event_type,
                handler_full_name=f"{module_path}_{name}",
                handler_name=name,
                handler_module_path=module_path,
                handler=raw,
                event_filters=[],
            )
            star_handlers_registry.append(md)

        result = plugin._repair_private_companion_lifecycle_bindings()
        check("生命周期绑定修复报告成功", result is not None, repr(result))
        check("重复修复幂等", plugin._repair_private_companion_lifecycle_bindings() is None)

        metadata = SimpleNamespace(name="some-plugin")
        for handler in star_handlers_registry:
            if handler.handler_module_path == module_path and handler.handler_name in (
                "_on_external_plugin_loaded", "_on_external_plugin_unloaded",
            ):
                await handler.handler(metadata)
        check(
            "修复后两个钩子均可调用",
            calls == [("loaded", metadata), ("unloaded", metadata)],
            repr(calls),
        )
    finally:
        star_handlers_registry._handlers[:] = old_handlers
        star_handlers_registry.star_handlers_map.clear()
        for handler in old_handlers:
            star_handlers_registry.star_handlers_map[handler.handler_full_name] = handler
        if old_meta is None:
            star_map.pop(module_path, None)
        else:
            star_map[module_path] = old_meta


async def test_private_companion_lifecycle_deferred_binding(plugin):
    print("\n[9] Private Companion 生命周期钩子：metadata 未注册时延迟、绑定后转发")
    from astrbot.core.star.star import StarMetadata, star_map
    from astrbot.core.star.star_handler import (
        EventType, StarHandlerMetadata, star_handlers_registry,
    )

    module_path = "data.plugins.astrbot_plugin_private_companion.main"
    old_meta = star_map.get(module_path)
    old_handlers = list(star_handlers_registry)
    calls = []

    class FakePrivateCompanion:
        async def _on_external_plugin_loaded(self, metadata, *args, **kwargs):
            calls.append(("loaded", metadata))

    instance = FakePrivateCompanion()
    star_map.pop(module_path, None)
    try:
        handler = StarHandlerMetadata(
            event_type=EventType.OnPluginLoadedEvent,
            handler_full_name=f"{module_path}_raw_lifecycle",
            handler_name="_on_external_plugin_loaded",
            handler_module_path=module_path,
            handler=FakePrivateCompanion._on_external_plugin_loaded,
            event_filters=[],
        )
        star_handlers_registry.append(handler)

        result = plugin._repair_private_companion_lifecycle_bindings()
        check("metadata 未注册时仍安装延迟转发", result is not None, repr(result))
        check(
            "重复修复延迟包装幂等",
            plugin._repair_private_companion_lifecycle_bindings() is None,
        )
        external_metadata = SimpleNamespace(name="some-plugin")
        await handler.handler(external_metadata)
        check("实例未就绪前调用不抛错且不传递", calls == [], repr(calls))

        # Private Companion registers metadata only after the raw handlers have
        # already reached the registry. Resolve the instance by module suffix.
        star_map[module_path] = StarMetadata(
            name="astrbot_plugin_private_companion",
            module_path=module_path,
            star_cls_type=FakePrivateCompanion,
            star_cls=instance,
        )
        await handler.handler(external_metadata)
        check(
            "实例注册后延迟包装自动转发",
            calls == [("loaded", external_metadata)],
            repr(calls),
        )

        # AstrBot's plugin loader binds the wrapper with functools.partial once
        # Private Companion has instantiated. It must then reach the real hook.
        handler.handler = functools.partial(handler.handler, instance)
        await handler.handler(external_metadata)
        check(
            "AstrBot 绑定实例后正常转发",
            calls == [("loaded", external_metadata), ("loaded", external_metadata)],
            repr(calls),
        )
        check(
            "绑定后再次修复幂等",
            plugin._repair_private_companion_lifecycle_bindings() is None,
        )
    finally:
        star_handlers_registry._handlers[:] = old_handlers
        star_handlers_registry.star_handlers_map.clear()
        for item in old_handlers:
            star_handlers_registry.star_handlers_map[item.handler_full_name] = item
        if old_meta is None:
            star_map.pop(module_path, None)
        else:
            star_map[module_path] = old_meta


async def main():
    plugin = load_plugin()
    upstream = AM._process_quote_message
    old_extract = AM.extract_quoted_message_text

    async def fake_extract(event, quote, **kwargs):
        return getattr(quote, "message_str", "") or "被引用的原话"

    AM.extract_quoted_message_text = fake_extract
    try:
        await test_success_and_identity(plugin, upstream)
        await test_empty_and_exception(plugin, upstream)
        await test_timeout(plugin, upstream)
        await test_astrna_wrapper_chain(plugin, upstream)
        await test_wrapped_quote_function(plugin, upstream)
        test_marker_walker(plugin)
        await test_astrna_montage_compat(plugin)
        await test_private_companion_lifecycle_repair(plugin)
        await test_private_companion_lifecycle_deferred_binding(plugin)
    finally:
        AM._process_quote_message = upstream
        AM.extract_quoted_message_text = old_extract

    print("\n" + "=" * 64)
    print(f"结果: {'✅ 全部通过' if FAIL == 0 else f'❌ {FAIL} 项失败'}  ({PASS}/{PASS + FAIL})")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))