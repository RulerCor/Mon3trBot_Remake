# -*- coding: utf-8 -*-
"""Mon3trBot_Remake runtime patch plugin for AstrBot.

This plugin keeps Mon3trBot-specific behavior in one place instead of editing
AstrBot's installed source files. Every patch is applied in memory when the
plugin starts, so upgrading AstrBot does not require replaying source patches.

Runtime patches:
  1. provider_getkeys       normalize a string API key to a one-item list
  2. openai_read_timeout    bound stalled OpenAI-compatible streaming reads
  3. kb_wording_mgr         label retrieved knowledge as reference material
  4. kb_wording_agent       apply the same rule to non-agentic KB injection
  5. kb_scope               expose the knowledge-base scope to API keys
  6. aiocqhttp_primary      route proactive sends without an event context
  7. cron_sender_name       use a neutral sender name for scheduler events
  8. group_alias_note       explain nickname/remark aliases in group context
  9. active_reply_blacklist keep the 4.29-removed group blacklist behavior
 10. humanizer_session_style apply per-session humanizer style overrides
 11. event_loop_hotpaths cache blocking plugin discovery/snapshot helpers
 12. hook_chain_timing add request-scoped timing logs to AstrBot hook dispatch
 13. context_budget      make skills-like schemas and bounded memory hooks upgrade-safe
 14. astrna_prompt_cache  enable dynamic system-prompt migration at runtime

The active-reply blacklist and humanizer style overrides are stored under
AstrBot's plugin_data directory.
Persona rules live in data_v4.db and are checked separately by
scripts/check_patches.py; they are not modified by this plugin.
"""

from contextvars import ContextVar
import asyncio
import hashlib
import inspect
import io
import json
import os
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import filter as event_filter, AstrMessageEvent
from astrbot.api.star import Context, Star

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

ACTIVE_REPLY_BLACKLIST_PATH = os.path.normpath(os.path.join(
    PLUGIN_DIR, "..", "..", "plugin_data", "akasha_rc_patches",
    "active_reply_blacklist.json",
))
HUMANIZER_STYLE_OVERRIDES_PATH = os.path.normpath(os.path.join(
    PLUGIN_DIR, "..", "..", "plugin_data", "akasha_rc_patches",
    "humanizer_style_overrides.json",
))
HUMANIZER_PLUGIN_NAME = "astrbot_plugin_wanna_be_human"
HUMANIZER_PATCH_KEY = "humanizer_session_style"
HOTPATH_PLUGIN_NAMES = (
    "astrbot_plugin_private_companion",
    "astrbot_plugin_image_companion",
)
HOTPATH_PATCH_KEY = "event_loop_hotpaths"
_HOOK_TIMING_PATCH_KEY = "hook_chain_timing"
_HOOK_TIMING_SLOW_MS = 1000
_HOTPATH_PENDING_PATCHES = {HOTPATH_PATCH_KEY}
_HUMANIZER_ACTIVE_STYLE = ContextVar("mon3tr_humanizer_active_style", default="")


def _read_json(path, default=None):
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── 识图（image caption）统一超时预算 ────────────────────────────
# 事故（2026-10-29）：内建群上下文识图曾单次阻塞钩子 21s；主回复路径
# （astr_main_agent._request_img_caption / _process_quote_message）更是
# 完全无超时上限，provider 自身 timeout=120s + 重试可让单条回复卡住
# 数十秒到两分钟。这里给所有识图调用一个统一上限，超时走各自原有的
# 失败分支（能力不降级：只把「等不到」变成「明确失败」）。
_CAPTION_TIMEOUT_DEFAULT = 12.0
_CAPTION_TIMEOUT_CONFIG_KEY = "image_caption_timeout_seconds"
_CAPTION_TIMEOUT_CACHE = {}


def _caption_timeout_seconds():
    """Resolve the caption timeout, allowing an optional config override."""
    try:
        cache = _CAPTION_TIMEOUT_CACHE
        now = time.monotonic()
        hit = cache.get("value")
        if hit and hit[0] > now:
            return hit[1]
        value = _CAPTION_TIMEOUT_DEFAULT
        cfg = _read_json(os.path.join(PLUGIN_DIR, "_conf_schema.json"), {})
        overrides = _read_json(
            os.path.normpath(os.path.join(
                PLUGIN_DIR, "..", "..", "config",
                "akasha_rc_patches_config.json",
            )),
            {},
        ) or {}
        candidate = overrides.get(_CAPTION_TIMEOUT_CONFIG_KEY)
        if candidate is None and isinstance(cfg, dict):
            candidate = cfg.get(_CAPTION_TIMEOUT_CONFIG_KEY)
        try:
            if candidate is not None:
                num = float(candidate)
                if 1.0 <= num <= 120.0:
                    value = num
        except (TypeError, ValueError):
            pass
        cache["value"] = (now + 30.0, value)
        return value
    except Exception:
        return _CAPTION_TIMEOUT_DEFAULT


def _legacy_active_reply():
    cfg_path = os.path.normpath(os.path.join(
        PLUGIN_DIR, "..", "..", "cmd_config.json"))
    cfg = _read_json(cfg_path, {}) or {}
    ar = ((cfg.get("provider_ltm_settings") or {}).get("active_reply") or {})
    return ar if isinstance(ar, dict) else {}


def _known_group_ids():
    """Read bridge contact cache and reproduce its stable integer-group IDs."""
    cfg_path = os.path.normpath(os.path.join(
        PLUGIN_DIR, "..", "..", "cmd_config.json"))
    repo = os.path.abspath(os.path.join(os.path.dirname(cfg_path), "..", "..", ".."))
    chat_path = os.path.join(repo, "runtime", "bridge", "data", "chat_names.json")
    chat = _read_json(chat_path, {}) or {}
    out = []
    for wxid in chat:
        if "@chatroom" not in str(wxid):
            continue
        digest = hashlib.md5(str(wxid).encode("utf-8")).digest()
        out.append(str(int.from_bytes(digest[:8], "big") % (2 ** 31)))
    return list(dict.fromkeys(out))


def _runtime_blacklist():
    """Return the canonical active-reply blacklist.

    AstrBot 4.29 removed active_reply.blacklist from core. The plugin owns this
    file, so upgrades and config migrations cannot silently drop the setting.
    On first use, migrate the old blacklist. If an old whitelist was present,
    preserve its effective behavior by blacklisting every known group outside
    that whitelist, then clear the obsolete whitelist in the panel.
    """
    path = ACTIVE_REPLY_BLACKLIST_PATH
    if os.path.isfile(path):
        data = _read_json(path, None)
        if isinstance(data, dict):
            data = data.get("entries") or []
        return [str(x).strip() for x in (data or []) if str(x).strip()]

    legacy = _legacy_active_reply()
    raw = legacy.get("blacklist") or []
    entries = [str(x).strip() for x in raw if str(x).strip()]
    whitelist = {str(x).strip() for x in (legacy.get("whitelist") or []) if str(x).strip()}
    if whitelist:
        known = _known_group_ids()
        # Match the old precedence: whitelist restricts candidates, then the
        # explicit blacklist excludes them. With a non-empty whitelist, all
        # known groups outside it are effectively blacklisted.
        entries = [gid for gid in known if gid not in whitelist]
    if entries:
        try:
            _atomic_write_json(path, {"entries": list(dict.fromkeys(entries))})
            logger.info(f"[Mon3trBot补丁][主动回复] 已迁移并接管 {len(set(entries))} 条黑名单")
        except Exception as e:
            logger.warning(f"[Mon3trBot补丁][主动回复] 黑名单迁移失败：{e}")
    return list(dict.fromkeys(entries))


# ══════════════════════════════════════════════════════════════════
# 补丁实现（每个函数返回 (模块名, 属性名, 新值, 描述)；失败抛异常）
# ══════════════════════════════════════════════════════════════════

def _patch_hook_chain_timing():
    """Wrap every registered on_llm_request handler with a timing log.

    Wrapping ``call_event_hook`` proved unreliable because several AstrBot
    releases bind it with ``from ... import`` and because plugin instances keep
    their own copies. Wrapping the handler objects themselves is immune to that
    and always takes effect. Handlers slower than one second are logged with
    plugin name, handler name, priority and elapsed time.
    """
    try:
        from astrbot.core.star.star_handler import (
            star_handlers_registry,
            EventType,
        )
        from astrbot.core.star.star import star_map
    except Exception:
        return None
    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnLLMRequestEvent
        )
    except Exception:
        return None
    patched = 0
    for handler in list(handlers):
        original = getattr(handler, "handler", None)
        if not callable(original) or getattr(original, "_akasha_hook_timing", False):
            continue
        module_path = str(getattr(handler, "handler_module_path", "") or "")
        try:
            metadata = star_map.get(module_path)
            plugin_name = str(
                getattr(metadata, "name", "") or module_path
            )
        except Exception:
            plugin_name = module_path
        handler_name = str(getattr(handler, "handler_name", "") or "")
        try:
            priority = handler.extras_configs.get("priority", 0)
        except Exception:
            priority = 0

        def _make_timed(original_handler, p_name, h_name, prio):
            async def _timed_handler(*args, **kwargs):
                started = time.monotonic()
                try:
                    return await original_handler(*args, **kwargs)
                finally:
                    elapsed_ms = (time.monotonic() - started) * 1000
                    if elapsed_ms >= _HOOK_TIMING_SLOW_MS:
                        logger.info(
                            "[Mon3trBot补丁][钩子耗时] %s.%s priority=%s 完成 %.0fms",
                            p_name, h_name, prio, elapsed_ms,
                        )
            _timed_handler._akasha_hook_timing = True
            _timed_handler.__name__ = getattr(original_handler, "__name__", "_timed_handler")
            return _timed_handler

        handler.handler = _make_timed(original, plugin_name, handler_name, priority)
        patched += 1
    if not patched:
        return None
    return ("star_handlers_registry", "OnLLMRequestEvent",
            f"钩子耗时诊断已包住 {patched} 个 on_llm_request 处理器")


def _resolve_group_chat_context_cls():
    """Return the *live* GroupChatContext class.

    AstrBot loads its builtin ``astrbot`` star after third-party plugins, and a
    lazy import there can re-create the module and produce a brand-new class
    object that no longer carries our earlier patch. Always resolve through
    ``sys.modules`` first so we patch the class the running star actually uses.
    """
    import sys as _sys
    name = "astrbot.builtin_stars.astrbot.group_chat_context"
    module = _sys.modules.get(name)
    if module is None:
        try:
            import importlib
            module = importlib.import_module(name)
        except Exception:
            return None
    return getattr(module, "GroupChatContext", None)


def _resolve_history_block_formatter():
    import sys as _sys
    module = _sys.modules.get("astrbot.builtin_stars.astrbot.group_chat_context")
    return getattr(module, "_format_group_history_block", None)


def _patch_group_chat_caption_cache():
    """Cache (and bound) AstrBot's built-in group image captioning.

    AstrBot's built-in ``GroupChatContext._format_message`` calls a vision
    model for every inbound group image, on the request critical path. A single
    slow caption blocked ``astrbot.decorate_llm_req`` for 21s in production.
    This wrapper adds a content-addressed TTL cache plus in-flight de-duplication
    and a bounded timeout that falls back to the upstream error path, so a slow
    provider degrades the same way an upstream failure already does.
    """
    GroupChatContext = _resolve_group_chat_context_cls()
    if GroupChatContext is None:
        return None
    original = getattr(GroupChatContext, "get_image_caption", None)
    if not callable(original) or getattr(original, "_akasha_cached", False):
        return None
    cache = {}
    inflight = {}
    ttl = 86400.0
    timeout_seconds = _caption_timeout_seconds()

    async def get_image_caption(self, image_url, image_caption_provider_id, image_caption_prompt):
        key = (
            str(image_url or ""),
            str(image_caption_provider_id or ""),
            str(image_caption_prompt or ""),
        )
        if not key[0]:
            return await original(
                self, image_url, image_caption_provider_id, image_caption_prompt
            )
        now = time.monotonic()
        hit = cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        task = inflight.get(key)
        if task is None:
            async def _run():
                text = await original(
                    self, image_url, image_caption_provider_id, image_caption_prompt
                )
                # 空白描述不当成有效结果：不写缓存、不放进历史，
                # 否则会产出 "[Image: ]" 这种空描述。
                if text and str(text).strip():
                    cache[key] = (time.monotonic() + ttl, text)
                inflight.pop(key, None)
                return text
            task = asyncio.ensure_future(_run())
            inflight[key] = task
            task.add_done_callback(lambda _t, k=key: inflight.pop(k, None))
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "[Mon3trBot补丁][群图片描述] 上游 %.0fs 未返回，本轮跳过描述: url=%s",
                timeout_seconds,
                key[0][:120],
            )
            raise

    get_image_caption._akasha_cached = True
    get_image_caption.__name__ = "get_image_caption"
    GroupChatContext.get_image_caption = get_image_caption
    return ("astrbot.builtin_stars.astrbot.group_chat_context.GroupChatContext",
            "get_image_caption", "群图片描述缓存(24h)+12s 超时保护")


def _patch_group_context_lockfree_inject():
    """Avoid cross-message lock contention in AstrBot's group context inject.

    ``GroupChatContext.on_req_llm`` awaits the same per-session lock that
    ``handle_message`` holds while it awaits a vision caption. A slow caption on
    any inbound image therefore stalls the *reply* request hook for the same
    duration (observed: ``astrbot.decorate_llm_req`` = 21.4s). The inject path
    only reads and trims a deque, so it does not need the lock at all.
    """
    GroupChatContext = _resolve_group_chat_context_cls()
    if GroupChatContext is None:
        return None
    try:
        from astrbot.core.agent.message import TextPart
        _format_group_history_block = _resolve_history_block_formatter()
    except Exception:
        return None
    if _format_group_history_block is None:
        return None
    original = getattr(GroupChatContext, "on_req_llm", None)
    if not callable(original) or getattr(original, "_akasha_lockfree", False):
        return None

    async def on_req_llm(self, event, req):
        umo = event.unified_msg_origin
        record_id = event.get_extra("_group_context_record_id", None)
        prompt_idx = event.get_extra("_group_context_raw_idx", -1)
        if not isinstance(record_id, str) and (
            not isinstance(prompt_idx, int) or prompt_idx < 0
        ):
            return
        records = self.raw_records.get(umo)
        if not records:
            return
        raw_list = list(records)
        id_list = list(self._record_ids.get(umo, ()))
        if isinstance(record_id, str) and record_id in id_list:
            prompt_idx = id_list.index(record_id)
        if prompt_idx >= len(raw_list):
            return
        records_to_inject = raw_list[:prompt_idx]
        remaining = raw_list[prompt_idx + 1:]
        remaining_ids = id_list[prompt_idx + 1:] if id_list else []
        records.clear()
        records.extend(remaining)
        if id_list:
            record_ids = self._record_ids[umo]
            record_ids.clear()
            record_ids.extend(remaining_ids)
        if records_to_inject:
            req.extra_user_content_parts.append(
                TextPart(text=_format_group_history_block(records_to_inject))
            )

    on_req_llm._akasha_lockfree = True
    on_req_llm.__name__ = "on_req_llm"
    GroupChatContext.on_req_llm = on_req_llm
    return ("astrbot.builtin_stars.astrbot.group_chat_context.GroupChatContext",
            "on_req_llm", "群上下文注入改为无锁快照（消除跨消息锁等待）")


def _patch_main_agent_caption_timeout():
    """Bound the image-caption call on AstrBot's main reply path.

    AstrBot's built-in ``GroupChatContext.get_image_caption`` is already
    bounded by ``_patch_group_chat_caption_cache``. The reply path is not:
    ``astr_main_agent._ensure_img_caption`` calls ``_request_img_caption``,
    and ``_process_quote_message`` calls ``prov.text_chat`` directly — both
    await the caption provider with **no timeout of their own**, so a slow
    provider (its own timeout is 120s, plus retries) can stall a single
    reply for tens of seconds to two minutes. This wrapper adds one shared
    upper bound and lets each caller's existing ``except`` path run, which
    is exactly the same degradation as an upstream failure would produce.

    ``astr_main_agent`` calls ``_request_img_caption`` as a module-global,
    so rebinding the module attribute is sufficient. ``_process_quote_message``
    is imported with ``from ... import`` in some releases, so any already
    bound references are re-pointed as well.
    """
    try:
        from astrbot.core import astr_main_agent as mod
    except Exception:
        return None
    timeout_seconds = _caption_timeout_seconds()
    original = getattr(mod, "_request_img_caption", None)
    if not callable(original):
        return None
    if getattr(original, "_akasha_caption_timeout", False):
        return None

    async def _request_img_caption_bounded(*args, **kwargs):
        budget = _caption_timeout_seconds()
        try:
            return await asyncio.wait_for(
                original(*args, **kwargs), timeout=budget
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[Mon3trBot补丁][主回复识图] 上游 %.0fs 未返回，按失败降级",
                budget,
            )
            raise

    _request_img_caption_bounded._akasha_caption_timeout = True
    _request_img_caption_bounded.__name__ = "_request_img_caption"
    mod._request_img_caption = _request_img_caption_bounded

    # Re-point any already-bound references in every loaded module that
    # captured ``_request_img_caption`` or ``_process_quote_message``.
    rebound = 0
    import sys as _sys
    for _m in list(_sys.modules.values()):
        if _m is None:
            continue
        try:
            if getattr(_m, "_request_img_caption", None) is original:
                _m._request_img_caption = _request_img_caption_bounded
                rebound += 1
        except Exception:
            continue

    return ("astrbot.core.astr_main_agent", "_request_img_caption",
            f"主回复识图统一 {timeout_seconds:.0f}s 超时上限")

def _patch_context_budget_defaults():
    """Make skills-like schemas and bounded memory hooks upgrade-safe."""
    changed = []
    try:
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
        from astrbot.core.star.star import star_map
    except Exception:
        return None

    stage_cls = getattr(internal, "InternalAgentSubStage", None)
    original_initialize = getattr(stage_cls, "initialize", None) if stage_cls else None
    if callable(original_initialize) and not getattr(
        original_initialize, "_akasha_context_budget", False
    ):
        async def _initialize_with_budget(self, ctx):
            await original_initialize(self, ctx)
            self.tool_schema_mode = "skills_like"
            cfg = getattr(self, "main_agent_cfg", None)
            if cfg is not None:
                cfg.tool_schema_mode = "skills_like"
        _initialize_with_budget._akasha_context_budget = True
        _initialize_with_budget.__name__ = "initialize"
        stage_cls.initialize = _initialize_with_budget
        changed.append("AstrBot.tool_schema_mode=skills_like")

    for metadata in star_map.values():
        if str(getattr(metadata, "name", "") or "") != "astrbot_plugin_memory_companion":
            continue
        plugin = getattr(metadata, "star_cls", None)
        service = getattr(plugin, "service", None)
        config = getattr(service, "config", None)
        original_get = getattr(config, "get", None) if config is not None else None
        if callable(original_get) and not getattr(
            original_get, "_akasha_context_budget", False
        ):
            def _budget_get(dotted, default=None):
                value = original_get(dotted, default)
                if dotted == "hook_request_budget_seconds":
                    return 8.0
                if dotted == "retrieval_advanced.rerank_timeout_ms":
                    return 1200
                if dotted == "memory_injection.debug_stage_timing_enabled":
                    return True
                if dotted == "memory_summary.fallback_provider_id":
                    return "antie5/aggregate/deepseek-v4-1-flash"
                return value
            _budget_get._akasha_context_budget = True
            config.get = _budget_get
            changed.append("MemoryCompanion.hook_budget=8s")
            changed.append("MemoryCompanion.rerank_timeout=1.2s")
            changed.append("MemoryCompanion.stage_timing=on")
            changed.append("MemoryCompanion.summary_fallback=antie5-v4.1")
        break
    if not changed:
        return None
    # AstrBot imports this function into agent sub-stages via ``from ... import``;
    # patch both the module and already-bound references in both runner paths.
    try:
        import astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal as _internal_mod
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal as _internal_alias
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import third_party as _third_party
        for _mod in (_internal_mod, _internal_alias, _third_party):
            if hasattr(_mod, "InternalAgentSubStage"):
                setattr(_mod, "InternalAgentSubStage", stage_cls)
    except Exception:
        pass
    return ("runtime.context_budget", "defaults", "; ".join(changed))


def _patch_astrna_cache_optimization():
    """Enable AstrNa's existing dynamic-system-prompt migration at runtime.

    The upstream option defaults to false and its config file may be replaced
    during upgrades. Installing the existing module preserves all content while
    moving observed dynamic system-prompt additions to temporary user context,
    which keeps the stable prompt prefix cacheable.
    """
    try:
        from astrbot.core.star.star import star_map
    except Exception:
        return None
    for metadata in star_map.values():
        if str(getattr(metadata, "name", "") or "") != "astrbot_plugin_AstrNa":
            continue
        plugin = getattr(metadata, "star_cls", None)
        runtime = getattr(plugin, "runtime", None)
        module = getattr(runtime, "dynamic_system_prompt", None)
        if module is None:
            return None
        if getattr(module, "_wrapped_handlers", None):
            return ("AstrNa.dynamic_system_prompt", "install",
                    "动态 system prompt 迁移（已生效）")
        module.install()
        return ("AstrNa.dynamic_system_prompt", "install",
                "动态 system prompt 迁移到临时 user content")
    return None


def _humanizer_style_overrides():
    """Read session-specific humanizer styles without caching.

    The bridge panel writes this file atomically. Reading it on every request
    avoids an AstrBot restart after a style change and keeps fallback logic
    authoritative for sessions not listed here.
    """
    data = _read_json(HUMANIZER_STYLE_OVERRIDES_PATH, {}) or {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}
    out = {}
    for umo, style in sessions.items():
        umo = str(umo).strip()
        style = str(style or "").strip()
        if umo and style and style != "__global__":
            out[umo] = style
    return out


def _patch_humanizer_session_style():
    """Apply per-session style overrides around the humanizer's own hook.

    The humanizer supports one global ``active_style``.  This patch does not
    copy that plugin's injection logic: it wraps the original request hook and
    exposes a request-scoped override through ``_effective_active_style``.
    As a result, any future fixes in the humanizer's own hook keep working,
    while its style profile and style-bound retrieval KB follow the selected
    session style.  Invalid or missing profiles fall back to the global style.
    """
    try:
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:
        return None

    plugin = None
    for metadata in star_map.values():
        if getattr(metadata, "name", "") == HUMANIZER_PLUGIN_NAME:
            candidate = getattr(metadata, "star_cls", None)
            if candidate is not None:
                plugin = candidate
                break
    if plugin is None:
        return None

    required = ("_effective_active_style", "_find_profile_cached", "on_llm_request")
    if not all(callable(getattr(plugin, name, None)) for name in required):
        logger.warning("[Mon3trBot补丁][会话风格] 人类风格插件接口不完整，暂不接管")
        return None

    def _is_humanizer_handler(handler):
        if getattr(handler, "handler_name", "") != "on_llm_request":
            return False
        target = getattr(handler, "handler", None)
        owner = getattr(target, "__self__", None)
        if owner is not None:
            return owner is plugin
        # AstrBot binds plugin methods as functools.partial(raw, instance).
        # Never use the module path when a concrete owner exists: after a hot
        # reload that path still identifies the old instance.
        if hasattr(target, "args"):
            return bool(target.args) and target.args[0] is plugin
        module_path = getattr(handler, "handler_module_path", "")
        return module_path.endswith(f"{HUMANIZER_PLUGIN_NAME}.main")

    def _patched_handler_count():
        return sum(1 for handler in star_handlers_registry
                   if _is_humanizer_handler(handler))

    original_effective = plugin._effective_active_style
    original_hook = plugin.on_llm_request
    if (getattr(original_effective, "_mon3tr_session_style", False)
            or getattr(original_hook, "_mon3tr_session_style", False)):
        patched = _patched_handler_count()
        if patched:
            count = len(_humanizer_style_overrides())
            return (HUMANIZER_PLUGIN_NAME, "on_llm_request",
                    f"会话风格运行时接管（已生效，{patched} 个入口，{count} 个自定义会话）")
        return None

    def _effective_active_style(self=None):
        target = getattr(original_effective, "__self__", None) or self
        if target is None:
            return ""
        override = _HUMANIZER_ACTIVE_STYLE.get()
        if override:
            try:
                profile = target._find_profile_cached(override)
            except Exception:
                profile = None
            if profile is not None:
                return profile["name"]
        if getattr(original_effective, "__self__", None) is not None:
            return original_effective()
        return original_effective(target)

    _effective_active_style._mon3tr_session_style = True
    plugin._effective_active_style = _effective_active_style

    async def on_llm_request(self, event, req):
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        override = _humanizer_style_overrides().get(umo, "")
        token = _HUMANIZER_ACTIVE_STYLE.set(override)
        try:
            if getattr(original_hook, "__self__", None) is not None:
                await original_hook(event, req)
            else:
                await original_hook(self, event, req)
        finally:
            _HUMANIZER_ACTIVE_STYLE.reset(token)

    on_llm_request._mon3tr_session_style = True
    plugin.on_llm_request = on_llm_request

    # AstrBot invokes registry hooks as ``handler(event, ...)``.  A bound
    # method already supplies the instance, so use that shape directly.
    patched = 0
    for handler in star_handlers_registry:
        if not _is_humanizer_handler(handler):
            continue
        handler.handler = on_llm_request.__get__(plugin, type(plugin))
        patched += 1

    if not patched:
        # Do not leave a half-installed patch if this AstrBot version stores
        # handlers differently than expected.  The global humanizer remains
        # fully usable.
        plugin._effective_active_style = original_effective
        plugin.on_llm_request = original_hook
        return None

    count = len(_humanizer_style_overrides())
    return (HUMANIZER_PLUGIN_NAME, "on_llm_request",
            f"会话风格运行时接管（{patched} 个入口，{count} 个自定义会话）")


def _patch_provider_getkeys():
    """get_keys(): str key → [str]。修复 str.copy() 崩溃。"""
    from astrbot.core.provider import provider as mod

    original = mod.Provider.get_keys

    if getattr(original, "_akasha_getkeys_patched", False):
        return None  # 已打过

    def get_keys(self):
        keys = self.provider_config.get("key", [""])
        if isinstance(keys, str):
            keys = [keys] if keys else [""]
        return keys or [""]

    get_keys._akasha_getkeys_patched = True
    mod.Provider.get_keys = get_keys
    return ("astrbot.core.provider.provider", "Provider.get_keys", "归一化 str→[str]")


def _kb_format_context_replacement():
    """构造新的 format_context（kb_mgr）。返回闭包。

    措辞与源码补丁 patch_kb_wording.py 的 NEW_KB_MGR 一致。
    """
    def format_context(self, results) -> str:
        """把检索结果格式化为注入文本。

        [知识库措辞补丁·插件版] 如实标注为【知识库】资料 + 三条使用要求：
        ① 当客观资料，不说成自己的经历/回忆；
        ② 正常回复不出现"知识库/资料库/检索/文档"字样；
        ③ 用户直接问来源时才如实说明。
        """
        if not results:
            return ""
        lines = ["（以下是【知识库】检索到的资料，作为你回答时的事实依据。使用要求："
                 "① 当客观资料用，不要说成自己的亲身经历、回忆或见闻；"
                 "② 正常回复里不要出现“知识库 / 资料库 / 检索 / 文档”这类字眼，"
                 "直接把内容自然地讲出来即可；"
                 "③ 只有当用户主动问起你的资料来源时，才可以如实说明。）\n"]
        for i, result in enumerate(results, 1):
            try:
                text = result.content if hasattr(result, "content") else str(result)
            except Exception:
                text = str(result)
            lines.append(f"【资料 {i}】\n{text}\n")
        return "\n".join(lines)

    return format_context


def _patch_kb_wording_mgr():
    from astrbot.core.knowledge_base import kb_mgr as mod

    # 源码层已打措辞补丁则跳过（幂等：源码补丁和插件补丁二选一即可）
    try:
        cur_src = inspect.getsource(mod.KnowledgeBaseManager._format_context)
        if "【知识库】" in cur_src:
            return None  # 源码层已是新措辞
    except (OSError, TypeError):
        pass

    fn = _kb_format_context_replacement()
    fn._akasha_kbwording_patched = True
    mod.KnowledgeBaseManager._format_context = fn
    return ("astrbot.core.knowledge_base.kb_mgr",
            "KnowledgeBaseManager._format_context", "知识库资料措辞+三条要求")


def _patch_kb_wording_agent():
    """Replace the non-agentic knowledge-base injection prefix at runtime."""
    from astrbot.core import astr_main_agent as mod

    original = mod._apply_kb
    if getattr(original, "_akasha_kb_wording_agent_patched", False):
        return None

    async def _apply_kb_patched(event, req, plugin_context, config):
        await original(event, req, plugin_context, config)
        for part in getattr(req, "extra_user_content_parts", []):
            text = getattr(part, "text", "")
            if text.startswith("[Related Knowledge Base Results]:"):
                body = text.split(":", 1)[1].lstrip("\n")
                part.text = (
                    "（以下是你手头可查的知识库资料，作为回答的事实依据；"
                    "当客观资料使用，不要说成自己的经历或回忆；"
                    "回复里不要提“知识库/资料”这类字样，"
                    "用户直接问起来源时才如实说明。）\n" + body
                )

    _apply_kb_patched._akasha_kb_wording_agent_patched = True
    mod._apply_kb = _apply_kb_patched
    return ("astrbot.core.astr_main_agent", "_apply_kb",
            "非 agentic KB 注入措辞运行时接管")




def _patch_aiocqhttp_primary():
    """Runtime fallback for proactive sends without event context.

    This mirrors the old source patch, but lives in memory so an AstrBot
    upgrade can never leave site-packages modified or half-patched.
    """
    from aiocqhttp import api_impl

    original = api_impl.WebSocketReverseApi.call_action
    if getattr(original, "_akasha_aiocqhttp_primary_patched", False):
        return None

    async def call_action(self, action, **params):
        api_ws = None
        if params.get("self_id"):
            api_ws = self._api_clients.get(str(params["self_id"]))
        elif api_impl.event_ws and api_impl.event_ws in self._event_clients:
            api_ws = self._api_clients.get(api_impl.event_ws.headers["X-Self-ID"])
        elif len(self._api_clients) == 1:
            api_ws = tuple(self._api_clients.values())[0]

        if not api_ws:
            excluded = {
                x for x in os.environ.get(
                    "AKASHA_OB_EXCLUDE_SELF_IDS", "99009900"
                ).replace(" ", "").split(",") if x
            }
            candidates = [
                ws for sid, ws in self._api_clients.items() if sid not in excluded
            ]
            if len(candidates) == 1:
                api_ws = candidates[0]

        if not api_ws:
            raise api_impl.ApiNotAvailable

        seq = api_impl._SequenceGenerator.next()
        await api_ws.send(json.dumps({
            "action": action,
            "params": params,
            "echo": {"seq": seq},
        }))
        return api_impl._handle_api_result(
            await api_impl.ResultStore.fetch(seq, self._timeout_sec)
        )

    call_action._akasha_aiocqhttp_primary_patched = True
    api_impl.WebSocketReverseApi.call_action = call_action
    return ("aiocqhttp.api_impl", "WebSocketReverseApi.call_action",
            "主动发送路由运行时兜底")


def _patch_kb_scope():
    """Expose the kb scope to API keys without editing AstrBot files."""
    from astrbot.dashboard.services import auth_service as auth_mod
    from astrbot.dashboard.services import api_key_service as key_mod
    from astrbot.dashboard.api import auth as auth_api

    if "kb" in auth_mod.ALL_OPEN_API_SCOPES:
        return None
    scopes = tuple(auth_mod.DEFAULT_OPEN_API_SCOPES) + (
        auth_mod.CHAT_ADMIN_SCOPE,
        auth_mod.CONFIG_EDIT_ADMIN_SCOPE,
        "kb",
    )
    auth_mod.ALL_OPEN_API_SCOPES = scopes
    key_mod.ALL_OPEN_API_SCOPES = scopes
    auth_api.ALL_OPEN_API_SCOPES = scopes
    return ("dashboard.auth_service + auth + api_key_service",
            "ALL_OPEN_API_SCOPES", "kb scope 运行时放开")


def _patch_openai_read_timeout():
    """给 OpenAI 兼容 provider 的 httpx 客户端加 read timeout。

    事故（2026-09-20 00:02 前后）：openrouter 免费渠道偶发返回 chunked
    响应后**流未正常终止**，httpx 的 async for 永久等待下一个数据块——
    工具循环任务永久挂起，无异常、无超时、无回复（日志止于
    receive_response_body.started）。上游 create_proxy_client 创建的
    AsyncClient **不带 timeout**，SDK 级 timeout 对这种挂起不生效。

    修复：包装 create_proxy_client，所有 OpenAI 兼容 provider 的
    httpx.AsyncClient 强制携带 read timeout（默认 180s）——挂起的
    读体最多 3 分钟后抛 ReadTimeout，agent 上层会重试或报错，而非静默挂死。
    """
    from astrbot.core.utils import network_utils as nu

    if getattr(nu.create_proxy_client, "_akasha_timeout_patched", False):
        return None

    original = nu.create_proxy_client

    def create_proxy_client_with_timeout(provider_label, proxy=None,
                                         headers=None, verify=None,
                                         httpx_module=None):
        client = original(provider_label, proxy=proxy, headers=headers,
                          verify=verify, httpx_module=httpx_module)
        # 只对 LLM 主通道生效（OpenAI 标签）；嵌入通道（httpx2）保持原样
        if provider_label == "OpenAI":
            timeout = httpx_module.Timeout(connect=15.0, read=180.0,
                                           write=30.0, pool=30.0)
            client.timeout = timeout
        return client

    create_proxy_client_with_timeout._akasha_timeout_patched = True
    nu.create_proxy_client = create_proxy_client_with_timeout

    # openai_source 在模块顶部 from ... import create_proxy_client，
    # 已绑定到其模块命名空间——需要同步替换
    from astrbot.core.provider.sources import openai_source as oai
    if getattr(oai.create_proxy_client, "_akasha_timeout_patched", False) is not True:
        oai.create_proxy_client = create_proxy_client_with_timeout

    return ("astrbot/core/utils/network_utils.py + openai_source",
            "httpx read timeout", "read=180s（防流式响应挂起）")


def _patch_cron_sender_name():
    """Keep scheduler events from leaking a callable-looking sender name."""
    from astrbot.core.cron import events as mod

    original = mod.CronMessageEvent.__init__
    if getattr(original, "_akasha_cron_sender_patched", False):
        return None

    def __init__(self, *args, **kwargs):
        if kwargs.get("sender_name") == "Scheduler":
            kwargs["sender_name"] = "系统日程"
        return original(self, *args, **kwargs)

    __init__._akasha_cron_sender_patched = True
    mod.CronMessageEvent.__init__ = __init__
    return ("astrbot.core.cron.events", "CronMessageEvent.__init__",
            "默认发送者名中性化（系统日程）")


def _patch_active_reply_blacklist():
    """把 AstrBot 4.29 删除的主动回复黑名单恢复为运行时行为。

    名单放在插件自己的配置文件里，AstrBot 升级/配置迁移不会丢失。
    """
    from astrbot.builtin_stars.astrbot import group_chat_context as mod

    mine = _runtime_blacklist()
    original = mod.GroupChatContext.need_active_reply
    if getattr(original, "_akasha_ar_blacklist_patched", False):
        return None

    async def need_active_reply(self, event):
        # 先走 AstrBot 4.29 原逻辑（含白名单、唤醒、概率）。
        allowed = await original(self, event)
        if not allowed:
            return False
        # 4.29 删除了黑名单；黑名单优先级高于白名单，命中即拒绝。
        bl = _runtime_blacklist()
        if not bl:
            return True
        gid = event.get_group_id()
        return not (event.unified_msg_origin in bl or (gid and gid in bl))

    need_active_reply._akasha_ar_blacklist_patched = True
    mod.GroupChatContext.need_active_reply = need_active_reply
    return ("astrbot.builtin_stars.astrbot.group_chat_context",
            "need_active_reply", f"黑名单运行时接管（{len(mine)} 条）")


def _patch_group_alias_note():
    """Move bridge-provided <alias> hints into the group context header.

    The bridge already emits nickname=remark aliases on group messages. This
    wrapper keeps the same behavior without touching AstrBot's source file.
    """
    from astrbot.builtin_stars.astrbot import group_chat_context as mod

    original = mod._format_group_history_block
    if getattr(original, "_mon3tr_alias_note_patched", False):
        return None
    try:
        if "同人别名补丁" in inspect.getsource(original):
            return None
    except (OSError, TypeError):
        pass

    def _format_group_history_block(records: list[str]) -> str:
        import re
        pattern = re.compile(r"<alias>([^<]*)</alias>")
        found = {}
        cleaned = []
        for record in records:
            marks = pattern.findall(record or "")
            if marks:
                record = pattern.sub("", record).rstrip()
                for mark in marks:
                    for pair in mark.split(";"):
                        if "=" not in pair:
                            continue
                        alias, primary = (x.strip() for x in pair.split("=", 1))
                        if alias and primary and alias != primary:
                            found.setdefault(primary, set()).add(alias)
            if record:
                cleaned.append(record)
        note = ""
        if found:
            lines = [
                f"[同名说明] {'、'.join(sorted(aliases))} = {primary}"
                f"（同一个人；{primary} 是备注名）"
                for primary, aliases in found.items()
            ]
            note = "\n".join(lines) + "\n"
        return (mod.GROUP_HISTORY_HEADER + note
                + "\n".join(cleaned) + mod.GROUP_HISTORY_FOOTER)

    _format_group_history_block._mon3tr_alias_note_patched = True
    mod._format_group_history_block = _format_group_history_block
    return ("astrbot.builtin_stars.astrbot.group_chat_context",
            "_format_group_history_block", "同名说明运行时接管")



def _find_private_companion_api():
    """Return the live private companion extension API without a dashboard query."""
    try:
        from astrbot.core.star.star import star_map
    except Exception:
        star_map = {}
    api = None
    for metadata in star_map.values():
        if getattr(metadata, "name", "") != "astrbot_plugin_private_companion":
            continue
        plugin = getattr(metadata, "star_cls", None)
        api = getattr(plugin, "extension_api", None) if plugin is not None else None
        if api is not None:
            break
    if api is None:
        try:
            import astrbot_plugin_private_companion.main as companion_main
            api = companion_main.get_private_companion_api()
        except Exception:
            api = None
    if api is None:
        try:
            import data.plugins.astrbot_plugin_private_companion.main as companion_main
            api = companion_main.get_private_companion_api()
        except Exception:
            api = None
    if api is not None:
        lifecycle = getattr(api, "bridge_lifecycle_status", None)
        if callable(lifecycle):
            try:
                status = lifecycle()
            except Exception:
                return None
            if not isinstance(status, dict) or status.get("active") is not True:
                return None
    return api


def _patch_event_loop_hotpaths():
    """Apply event-loop latency fixes after their target plugins have loaded."""
    results = []
    for patch in (_patch_image_sdgen_lookup_cache, _patch_image_status_cache, _patch_req041_scoped_snapshot_async):
        try:
            result = patch()
        except Exception:
            result = None
        if result:
            results.append(result)
    if not results:
        return None
    return ("runtime-hotpaths", "+".join(item[1] for item in results),
            "; ".join(item[2] for item in results), len(results))


def _find_image_runtime_class():
    """Return the ImageGenerationRuntime class from the installed plugin."""
    try:
        from data.plugins.astrbot_plugin_image_companion.image_runtime import (
            ImageGenerationRuntime,
        )
        return ImageGenerationRuntime
    except Exception:
        return None


def _patch_image_status_cache():
    """Bound ImageCompanion's synchronous capability probe.

    ``ImageCompanionPlugin.status()`` constructs an ImageGenerationRuntime and
    calls capability_status() on the event-loop thread. That probe may scan the
    whole GC heap through SDGen discovery. Cache the resulting status for 30s
    while preserving explicit refresh paths.
    """
    try:
        from data.plugins.astrbot_plugin_image_companion.main import ImageCompanionPlugin
        from astrbot.core.star.star import star_map
    except Exception:
        return None
    # Patch the SDGen probe on the runtime class as well. This is the actual
    # hot path; replacing _find_sdgen_plugin alone can miss when the runtime
    # class is resolved through a different module alias.
    try:
        from data.plugins.astrbot_plugin_image_companion.image_runtime import (
            ImageGenerationRuntime,
        )
        original_avail = getattr(ImageGenerationRuntime, "_sdgen_photo_available", None)
        if callable(original_avail) and not getattr(
            original_avail, "_akasha_registry_only", False
        ):
            def _sdgen_photo_available(self):
                context = getattr(self, "context", None)
                getter = getattr(context, "get_registered_star", None)
                if callable(getter):
                    for name in ("SDGen", "astrbot_plugin_sdgen"):
                        try:
                            plugin = getter(name)
                        except Exception:
                            plugin = None
                        if plugin is not None and callable(
                            getattr(plugin, "_call_t2i_api", None)
                        ):
                            return True
                return False
            _sdgen_photo_available._akasha_registry_only = True
            ImageGenerationRuntime._sdgen_photo_available = _sdgen_photo_available
    except Exception:
        pass
    original_cls = getattr(ImageCompanionPlugin, "status", None)
    if callable(original_cls) and not getattr(original_cls, "_akasha_status_cached", False):
        cache = {"ts": 0.0, "value": None}
        def _status_cls(self):
            now = time.monotonic()
            if cache["value"] is not None and now - cache["ts"] <= 30.0:
                return dict(cache["value"])
            value = original_cls(self)
            if isinstance(value, dict):
                cache.update(ts=now, value=dict(value))
            return value
        _status_cls._akasha_status_cached = True
        _status_cls.__name__ = "status"
        ImageCompanionPlugin.status = _status_cls
    for metadata in star_map.values():
        if str(getattr(metadata, "name", "") or "") != "astrbot_plugin_image_companion":
            continue
        instance = getattr(metadata, "star_cls", None)
        if instance is None:
            continue
        original_inst = getattr(instance, "status", None)
        if callable(original_inst) and not getattr(original_inst, "_akasha_status_cached", False):
            cache = {"ts": 0.0, "value": None}
            def _status_inst():
                now = time.monotonic()
                if cache["value"] is not None and now - cache["ts"] <= 30.0:
                    return dict(cache["value"])
                value = original_inst()
                if isinstance(value, dict):
                    cache.update(ts=now, value=dict(value))
                return value
            _status_inst._akasha_status_cached = True
            _status_inst.__name__ = "status"
            instance.status = _status_inst
        return ("astrbot_plugin_image_companion.ImageCompanionPlugin", "status",
                "生图能力探测 30s 缓存")
    return ("astrbot_plugin_image_companion.ImageCompanionPlugin", "status",
            "生图能力探测 30s 缓存（类级）")


def _patch_image_sdgen_lookup_cache():
    """Replace ImageCompanion's GC-wide SDGen discovery with a safe probe.

    The upstream fallback walks ``gc.get_objects()`` and has repeatedly blocked
    the event loop for 20-35s. AstrBot's registry is authoritative, so probe it
    directly and return None when SDGen is absent. This preserves SDGen support
    without any GC scan or stale cache.
    """
    runtime_cls = _find_image_runtime_class()
    if runtime_cls is None:
        return None
    original = getattr(runtime_cls, "_find_sdgen_plugin", None)
    if not callable(original) or getattr(original, "_akasha_registry_only", False):
        return None

    def _find_sdgen_plugin(self):
        context = getattr(self, "context", None)
        getter = getattr(context, "get_registered_star", None)
        if callable(getter):
            for name in ("SDGen", "astrbot_plugin_sdgen"):
                try:
                    plugin = getter(name)
                except Exception:
                    plugin = None
                if plugin is not None and callable(getattr(plugin, "_call_t2i_api", None)):
                    return plugin
        return None

    _find_sdgen_plugin._akasha_registry_only = True
    _find_sdgen_plugin.__name__ = "_find_sdgen_plugin"
    runtime_cls._find_sdgen_plugin = _find_sdgen_plugin
    try:
        from data.plugins.astrbot_plugin_image_companion import main as image_main
        image_main.ImageGenerationRuntime = runtime_cls
        original_sdgen_available = getattr(runtime_cls, "_sdgen_photo_available", None)
        if callable(original_sdgen_available) and not getattr(
            original_sdgen_available, "_akasha_registry_only", False
        ):
            def _sdgen_photo_available(self):
                return runtime_cls._find_sdgen_plugin(self) is not None
            _sdgen_photo_available._akasha_registry_only = True
            runtime_cls._sdgen_photo_available = _sdgen_photo_available
    except Exception:
        pass
    logger.info(
        "[Mon3trBot补丁][SDGen] 已替换 GC 全扫探针: class=%s func=%s",
        runtime_cls.__module__, runtime_cls._find_sdgen_plugin.__name__,
    )
    return ("astrbot_plugin_image_companion.image_runtime.ImageGenerationRuntime",
            "_find_sdgen_plugin", "SDGen 探测仅查注册表（移除 GC 全扫）")


def _patch_req041_scoped_snapshot_async():
    """Move the legacy snapshot copy required by REQ-041 off the event loop.

    ``_req041_sync_scoped_now`` kept ``deepcopy(default_data)`` and all persona
    profiles inside the plugin data lock.  In this deployment that call occurs on
    every scheduled persistence save.  The watchdog repeatedly showed the event
    loop stopped inside this copy, delaying unrelated chat replies.  We preserve
    the same data lock and the same snapshot helper, but execute it in a worker
    thread so the loop stays responsive while the copy is running.
    """
    api = _find_private_companion_api()
    if api is None:
        return None
    original = getattr(api, "_req041_sync_scoped_now", None)
    snapshot_fn = getattr(api, "_req041_legacy_snapshots_locked", None)
    if (
        not callable(original)
        or not callable(snapshot_fn)
        or getattr(original, "_akasha_async_snapshot", False)
    ):
        return None

    async def _req041_sync_scoped_now():
        synchronizer = getattr(api, "req041_scoped_projection_sync", None)
        if synchronizer is None:
            return {"ok": False, "code": "scoped_projection_not_initialized", "scopes": []}
        synchronizer.mark_dirty()
        async with api._data_lock:
            snapshots = await asyncio.to_thread(snapshot_fn)
        results = []
        for source_scope, snapshot in snapshots:
            results.append(await asyncio.to_thread(
                synchronizer.sync_snapshot, snapshot, source_scope=source_scope
            ))
        ok = all(item.get("ok") is True for item in results)
        summary = {
            "ok": ok,
            "code": "scoped_projection_synced" if ok else "scoped_projection_degraded",
            "scopes": results,
            "records": sum(int(item.get("records") or 0) for item in results),
            "errors": sum(int(item.get("errors") or 0) for item in results),
        }
        api.req041_scoped_projection_status = summary
        return summary

    _req041_sync_scoped_now._akasha_async_snapshot = True
    _req041_sync_scoped_now.__name__ = "_req041_sync_scoped_now"
    api._req041_sync_scoped_now = _req041_sync_scoped_now
    return ("astrbot_plugin_private_companion.extension_api",
            "_req041_sync_scoped_now", "REQ-041 快照复制移出事件循环")


PATCHES = [
    ("active_reply_blacklist", "主动回复黑名单运行时接管（4.29 回归修复）", _patch_active_reply_blacklist),
    ("provider_getkeys", "provider get_keys 归一化（修复 All chat models failed）", _patch_provider_getkeys),
    ("openai_read_timeout", "OpenAI 通道 read timeout（防流式响应挂起）", _patch_openai_read_timeout),
    ("kb_wording_mgr", "kb_mgr.format_context 知识库措辞", _patch_kb_wording_mgr),
    ("kb_wording_agent", "astr_main_agent 注入头运行时接管", _patch_kb_wording_agent),
    ("kb_scope", "kb scope 白名单（API key 调知识库接口）", _patch_kb_scope),
    ("aiocqhttp_primary", "aiocqhttp 主动发送路由运行时兜底", _patch_aiocqhttp_primary),
    ("cron_sender_name", "Cron 合成发送者名中性化（防 Scheduler博士泄漏）", _patch_cron_sender_name),
    ("group_alias_note", "群聊同名说明（昵称=备注）", _patch_group_alias_note),
    ("humanizer_session_style", "人类风格注入按会话选择检索风格", _patch_humanizer_session_style),
    ("event_loop_hotpaths", "事件循环热路径：SDGen 发现缓存与 REQ-041 快照异步化", _patch_event_loop_hotpaths),
    (_HOOK_TIMING_PATCH_KEY, "请求钩子链耗时诊断（定位 30~60s 静默）", _patch_hook_chain_timing),
    ("context_budget", "压缩工具 schema 与记忆钩子预算（升级后自动恢复）", _patch_context_budget_defaults),
    ("astrna_prompt_cache", "动态 system prompt 迁移（升级后自动恢复缓存友好前缀）", _patch_astrna_cache_optimization),
    ("group_caption_cache", "群图片描述缓存（避免每条图片消息阻塞 20s+）", _patch_group_chat_caption_cache),
    ("group_context_lockfree", "群上下文注入无锁化（消除跨消息锁等待）", _patch_group_context_lockfree_inject),
    ("main_agent_caption_timeout", "主回复识图统一超时上限（防单条回复卡 1~2 分钟）", _patch_main_agent_caption_timeout),
]
# These targets load after the patch plugin; retry them from on_plugin_loaded.
LATE_PATCH_KEYS = {
    HUMANIZER_PATCH_KEY,
    HOTPATH_PATCH_KEY,
    "context_budget",
    "astrna_prompt_cache",
    "group_caption_cache",
    "group_context_lockfree",
    "main_agent_caption_timeout",
}


# ══════════════════════════════════════════════════════════════════
# 插件主体
# ══════════════════════════════════════════════════════════════════

class AkashaPatchesPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self.context = context
        self.applied = {}

    async def initialize(self):
        logger.info("=" * 56)
        logger.info("[Mon3trBot补丁] 开始应用核心补丁集 v1.3.5")
        logger.info("=" * 56)
        for key, desc, fn in PATCHES:
            if key in LATE_PATCH_KEYS:
                try:
                    result = fn()
                    if result:
                        self.applied[key] = result
                        logger.info(f"  ✅ {desc} → {result[2]}")
                    else:
                        waiting = "目标插件加载/接口就绪"
                        logger.info(f"  ⏳ {desc} → 等待{waiting}")
                except Exception as e:
                    logger.error(f"  ❌ {desc} → {type(e).__name__}: {e}")
                continue
            try:
                result = fn()
                if result:
                    self.applied[key] = result
                    logger.info(f"  ✅ {desc} → {result[2]}")
                else:
                    logger.info(f"  ⏭️ {desc} → 已生效/无需重复")
            except Exception as e:
                logger.error(f"  ❌ {desc} → {type(e).__name__}: {e}")
        logger.info(f"[Mon3trBot补丁] 完成：{len(self.applied)}/{len(PATCHES)} 项生效")
        logger.info("=" * 56)

    def _apply_hotpath_patch(self):
        """Apply the hot-path patch once every cooperating plugin is loaded."""
        result = _patch_event_loop_hotpaths()
        if not result:
            # Diagnose the exact late-binding blocker so this cannot silently
            # regress to the previous "waiting forever" state.
            api = _find_private_companion_api()
            image_api = getattr(api, "_image_companion_api_fresh", None)
            try:
                image = image_api() if callable(image_api) else None
            except Exception as exc:
                image = None
                logger.warning(f"[Mon3trBot补丁][事件循环热路径] Image API 解析失败: {type(exc).__name__}: {exc}")
            logger.info(
                "[Mon3trBot补丁][事件循环热路径] 目标接口状态: "
                f"private_api={'ready' if api is not None else 'pending'} "
                f"image_runtime={'ready' if image is not None else 'pending'}"
            )
            return None
        self.applied[HOTPATH_PATCH_KEY] = result
        if int(result[3]) >= 2:
            _HOTPATH_PENDING_PATCHES.discard(HOTPATH_PATCH_KEY)
        logger.info(f"  ✅ 事件循环热路径：SDGen 发现缓存与 REQ-041 快照异步化 → {result[2]}")
        return result

    @event_filter.on_plugin_loaded()
    async def _on_plugin_loaded(self, metadata):
        """Apply late patches after their target plugins have fully loaded."""
        # PrivateCompanion invalidates its optional bridge caches here. Keep
        # that behavior independent of the handler-repair code below: the
        # image plugin may load before the private companion publishes its API.
        if str(getattr(metadata, "name", "") or "") == "astrbot_plugin_image_companion":
            try:
                api = _find_private_companion_api()
                invalidate = getattr(api, "_image_companion_api_fresh", None)
                if callable(invalidate):
                    invalidate()
            except Exception:
                pass
        name = str(getattr(metadata, "name", "") or "")
        # These two patch classes owned by AstrBot's builtin star, which loads
        # after this plugin. Re-apply on every load event; both are idempotent.
        for _key, _fn in (
            ("group_caption_cache", _patch_group_chat_caption_cache),
            ("group_context_lockfree", _patch_group_context_lockfree_inject),
            ("main_agent_caption_timeout", _patch_main_agent_caption_timeout),
        ):
            try:
                _result = _fn()
            except Exception:
                _result = None
            if _result:
                self.applied[_key] = _result
        # Re-wrap newly registered request hooks so the timing diagnostic covers
        # plugins that finish loading after this patch plugin.
        try:
            result = _patch_hook_chain_timing()
            if result:
                self.applied[_HOOK_TIMING_PATCH_KEY] = result
        except Exception:
            pass
        if name == "astrbot_plugin_memory_companion":
            try:
                result = _patch_context_budget_defaults()
            except Exception as e:
                logger.error(f"[Mon3trBot补丁][上下文预算] 延迟接管失败：{type(e).__name__}: {e}")
            else:
                if result:
                    self.applied["context_budget"] = result
                    logger.info(f"  ✅ 压缩工具 schema 与记忆钩子预算 → {result[2]}")
        if name == "astrbot_plugin_AstrNa":
            try:
                runtime = getattr(getattr(metadata, "star_cls", None), "runtime", None)
                module = getattr(runtime, "dynamic_system_prompt", None)
                if module is not None:
                    module.install()
                result = self.applied.get("astrna_prompt_cache")
                if result is None:
                    result = ("AstrNa.dynamic_system_prompt", "install",
                              "动态 system prompt 迁移已安装")
                self.applied["astrna_prompt_cache"] = result
                logger.info(f"  ✅ 动态 system prompt 迁移 → {result[2]}")
            except Exception as e:
                logger.error(f"[Mon3trBot补丁][提示词缓存] 延迟接管失败：{type(e).__name__}: {e}")
        if name == HUMANIZER_PLUGIN_NAME:
            try:
                result = _patch_humanizer_session_style()
            except Exception as e:
                logger.error(f"[Mon3trBot补丁][会话风格] 延迟接管失败：{type(e).__name__}: {e}")
                return
            if result:
                self.applied[HUMANIZER_PATCH_KEY] = result
                logger.info(f"  ✅ 人类风格注入按会话选择检索风格 → {result[2]}")
        if HOTPATH_PATCH_KEY in _HOTPATH_PENDING_PATCHES:
            try:
                self._apply_hotpath_patch()
                # The extension API publishes only after initialize(), which
                # AstrBot runs before emitting this hook. The image bridge may
                # additionally cache a negative result while the image plugin
                # is still finishing startup. Use the fresh resolver once and
                # give activation a bounded chance instead of leaving this patch
                # permanently pending.
                # One delayed task is sufficient. Repeated hook invocations
                # each retry for a bounded window; after that the status command
                # and the next plugin reload can retry without spamming logs.
                if not getattr(self, "_hotpath_retry_task", None):
                    async def _retry_hotpath():
                        for _ in range(180):
                            if HOTPATH_PATCH_KEY not in _HOTPATH_PENDING_PATCHES:
                                return
                            await asyncio.sleep(1.0)
                            self._apply_hotpath_patch()
                        # ImageCompanion may legitimately be unavailable when
                        # PrivateCompanion is absent. REQ-041 does not depend
                        # on it, so apply the completed hotpath patch instead
                        # of reporting a partial patch forever.
                        req041 = _patch_req041_scoped_snapshot_async()
                        if req041:
                            self.applied[HOTPATH_PATCH_KEY] = (
                                "runtime-hotpaths", req041[1], req041[2], 1,
                            )
                            _HOTPATH_PENDING_PATCHES.discard(HOTPATH_PATCH_KEY)
                            logger.info(
                                "  ✅ 事件循环热路径：REQ-041 快照异步化已生效；Image/SDGen 未安装或暂不可用"
                            )
                    self._hotpath_retry_task = asyncio.create_task(
                        _retry_hotpath(), name="akasha-hotpath-retry"
                    )
            except Exception as e:
                logger.error(f"[Mon3trBot补丁][事件循环热路径] 延迟接管失败：{type(e).__name__}: {e}")

    @event_filter.command("akasha_patches")
    async def status_cmd(self, event: AstrMessageEvent):
        """/akasha_patches 查看补丁状态"""
        lines = ["Mon3trBot_Remake 核心补丁集状态："]
        for key, desc, _ in PATCHES:
            mark = "✅" if key in self.applied else (
                "⏳" if key in LATE_PATCH_KEYS else "⏭️"
            )
            lines.append(f"{mark} {desc}")
        lines.append(f"共 {len(self.applied)}/{len(PATCHES)} 项由本插件接管；"
                     "人设层 5 段补丁在 personas 表中（不受升级影响）。")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        # monkey-patch 进程内生效，进程退出即消失，无需还原
        logger.info("[Mon3trBot补丁] 插件卸载（内存补丁随之消失）")
