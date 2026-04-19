from pathlib import Path
import os
import re

# === 新增：防止系统代理拦截本地请求 ===
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

import asyncio
import shutil
import json
from datetime import datetime
from typing import Optional, Dict, List, Any
import traceback
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Record, File
from astrbot.api.message_components import Node, Plain, Image as CompImage
from astrbot.core.utils.session_waiter import session_waiter, SessionController
from astrbot.api import logger
from functools import partial
from gradio_client import Client

try:
    from .api import QQ_MUSIC_AVAILABLE
except ImportError:
    QQ_MUSIC_AVAILABLE = False

MODEL_ALIAS_SEPARATOR = "|||"

@register(
    "astrbot_plugin_matsuko_cover",
    "Matsuko",
    "RVC/SVC翻唱网易云歌曲（支持LLM智能调用）",
    "2.5.2",
    "https://github.com/sdfsfsk/matsuko_cover",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:7860/")
        self.svc_base_url = config.get("svc_base_url", "http://127.0.0.1:7866/")
        
        self.default_api = config.get("default_api", "netease_nodejs")
        self.nodejs_base_url = config.get("nodejs_base_url", "https://163api.qijieya.cn")
        self.qqmusic_api_url = config.get("qqmusic_api_url", "http://127.0.0.1:3300")
        self.enable_qqmusic = config.get("enable_qqmusic", True)
        self.disable_netease = config.get("disable_netease", False)
        self.timeout = config.get("timeout", 60)
        
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        self.svc_models_keywords = config.get("svc_models_keywords", [])
        
        self.enable_rvc = config.get("enable_rvc", True)
        self.enable_svc = config.get("enable_svc", True)
        
        self.inference_timeout = config.get("inference_timeout", 300)
        self.llm_tool_timeout = config.get("llm_tool_timeout", 10)
        
        if self.default_api == "netease":
            from .api import NetEaseMusicAPI
            self.api = NetEaseMusicAPI()
        elif self.default_api == "netease_nodejs":
            from .api import NetEaseMusicAPINodeJs
            self.api = NetEaseMusicAPINodeJs(base_url=self.nodejs_base_url)
        elif self.default_api == "qqmusic":
            if not self.enable_qqmusic:
                logger.warning("QQ音乐功能未启用，但default_api设置为qqmusic，已自动启用")
                self.enable_qqmusic = True
            from .api import QQMusicAPI
            self.api = QQMusicAPI()
        else:
            logger.warning(f"未知的音乐API类型: {self.default_api}，使用默认的 netease_nodejs")
            from .api import NetEaseMusicAPINodeJs
            self.api = NetEaseMusicAPINodeJs(base_url=self.nodejs_base_url)

        # === 新增配置项 ===
        self.llm_force_mode = config.get("llm_force_mode", False)
        self.enable_smart_cover = config.get("enable_smart_cover", True)
        self.enable_enhanced_context = config.get("enable_enhanced_context", True)
        self.enable_confirm_mechanism = config.get("enable_confirm_mechanism", True)
        self.enable_batch_cover = config.get("enable_batch_cover", True)
        self.enable_preference_learning = config.get("enable_preference_learning", True)
        
        self.enable_progress_bar = config.get("enable_progress_bar", True)
        self.progress_update_interval = config.get("progress_update_interval", 3)
        self.enable_llm_success_notify = config.get("enable_llm_success_notify", True)
        self.enable_send_file = config.get("enable_send_file", False)
        self.enable_config_report = config.get("enable_config_report", True)
        
        self.default_api_type = config.get("default_api_type", "rvc")
        self.default_model_index = config.get("default_model_index", 1)
        self.default_key_shift = config.get("default_key_shift", 0)
        self.f0_method = config.get("f0_method", "rmvpe")
        self.svc_f0_method = config.get("svc_f0_method", "fcpe")
        self.index_rate = float(config.get("index_rate", "0.75"))
        self.filter_radius = config.get("filter_radius", 3)
        self.reverb_intensity = config.get("reverb_intensity", 4)
        self.delay_intensity = config.get("delay_intensity", 0)
        
        # === MSST 分离参数 ===
        self.msst_batch_size = config.get("msst_batch_size", 2)
        self.msst_num_overlap = config.get("msst_num_overlap", 4)
        self.msst_normalize = config.get("msst_normalize", True)
        
        self.max_batch_size = config.get("max_batch_size", 5)
        self.preference_storage_path = config.get("preference_storage_path", "data/user_preferences.json")

        # === 方案E：偏好学习系统 ===
        self.user_preferences: Dict[str, Dict] = {}
        self._load_preferences()

    async def _send_cover_result(self, event: AstrMessageEvent, result_path: str, song_name: str = "翻唱"):
        if not result_path or not os.path.exists(result_path):
            await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
            return
        await event.send(event.chain_result([Record(file=result_path)]))
        if self.enable_send_file:
            try:
                file_name = os.path.basename(result_path)
                name, ext = os.path.splitext(file_name)
                if not ext:
                    ext = ".mp3"
                safe_name = re.sub(r'[\\/:*?"<>|]', '', song_name)
                send_name = f"{safe_name}{ext}"
                await event.send(event.chain_result([File(name=send_name, file=result_path)]))
            except Exception as e:
                logger.error(f"以文件形式发送翻唱结果失败: {e}")

    def _load_preferences(self):
        """加载用户偏好数据"""
        if not self.enable_preference_learning:
            return
            
        try:
            pref_path = Path(self.preference_storage_path)
            if pref_path.exists():
                with open(pref_path, 'r', encoding='utf-8') as f:
                    self.user_preferences = json.load(f)
                logger.info(f"已加载用户偏好数据: {len(self.user_preferences)} 个用户")
        except Exception as e:
            logger.error(f"加载偏好数据失败: {e}")
            self.user_preferences = {}

    def _save_preferences(self):
        """保存用户偏好数据"""
        if not self.enable_preference_learning:
            return
            
        try:
            pref_path = Path(self.preference_storage_path)
            pref_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(pref_path, 'w', encoding='utf-8') as f:
                json.dump(self.user_preferences, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存用户偏好数据: {len(self.user_preferences)} 个用户")
        except Exception as e:
            logger.error(f"保存偏好数据失败: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识"""
        return str(event.get_sender_id())

    def _get_user_pref(self, user_id: str) -> Dict:
        """获取用户偏好，如果不存在则创建默认值"""
        if user_id not in self.user_preferences:
            self.user_preferences[user_id] = {
                "default_api_type": self.default_api_type,
                "default_model_index": self.default_model_index,
                "default_key_shift": self.default_key_shift,
                "favorite_songs": [],
                "usage_count": 0,
                "last_used_time": None,
                "preferred_artists": []
            }
        return self.user_preferences[user_id]

    async def _async_predict(self, client, *args, timeout=300, event=None, **kwargs):
        loop = asyncio.get_running_loop()
        job = client.submit(*args, **kwargs)
        
        if self.enable_progress_bar and event:
            import time
            import re as _re
            last_msg_time = 0
            last_desc = ""
            while not job.done():
                try:
                    status = job.status()
                    if hasattr(status, 'progress_data') and status.progress_data:
                        progress_data = status.progress_data[-1]
                        desc = getattr(progress_data, 'desc', None)
                        if desc is None and isinstance(progress_data, dict):
                            desc = progress_data.get('desc')
                        pct = None
                        if hasattr(progress_data, 'progress'):
                            try:
                                pct = float(progress_data.progress)
                            except (TypeError, ValueError):
                                pass
                        elif isinstance(progress_data, dict):
                            try:
                                pct = float(progress_data.get('progress', 0))
                            except (TypeError, ValueError):
                                pass
                        
                        current_time = time.time()
                        if desc and desc != last_desc:
                            if current_time - last_msg_time >= self.progress_update_interval or last_msg_time == 0:
                                has_sub_pct = bool(_re.search(r'\d+%', desc))
                                if has_sub_pct:
                                    msg = f"{self._get_stage_emoji(desc)} {desc}"
                                else:
                                    overall_pct = f" [{int(pct * 100)}%]" if pct is not None else ""
                                    msg = f"{self._get_stage_emoji(desc)} {desc}{overall_pct}"
                                asyncio.create_task(event.send(event.plain_result(msg)))
                                last_msg_time = current_time
                                last_desc = desc
                except Exception as e:
                    logger.debug(f"获取进度信息时出错: {e}")
                await asyncio.sleep(1)
                
        fn = partial(job.result, timeout=timeout)
        return await loop.run_in_executor(None, fn)

    def _get_stage_emoji(self, desc: str) -> str:
        if not desc:
            return "⏳"
        desc_lower = desc.lower()
        if any(kw in desc_lower for kw in ["下载", "download"]):
            return "📥"
        elif any(kw in desc_lower for kw in ["分离", "separat", "人声", "vocal", "msst", "bs-roformer", "uvr"]):
            return "🎤"
        elif any(kw in desc_lower for kw in ["推理", "infer", "rvc", "svc", "模型"]):
            return "🧠"
        elif any(kw in desc_lower for kw in ["混音", "mix", "处理音频", "混响", "reverb", "均衡", "伴奏"]):
            return "🎛️"
        elif any(kw in desc_lower for kw in ["导出", "export", "完成"]):
            return "✅"
        elif any(kw in desc_lower for kw in ["准备", "加载", "load", "缓存"]):
            return "⚙️"
        else:
            return "⏳"

    def get_models_display_list(self, api_type="rvc"):
        models_keywords = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = alias or os.path.splitext(model_name)[0]
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    def get_models_detailed_list(self, api_type="rvc"):
        """获取包含完整信息的模型列表（用于LLM匹配）"""
        models_keywords = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        detailed_info = []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            name_without_ext = os.path.splitext(model_name)[0]
            detailed_info.append({
                "index": index,
                "filename": model_name,
                "name": name_without_ext,
                "alias": alias,
                "display": alias or name_without_ext
            })
        return detailed_info

    def _find_model_index_by_name(self, model_name_input: str, api_type: str = "rvc") -> Optional[int]:
        """根据模型名称或别名查找模型序号（支持模糊匹配）"""
        if not model_name_input:
            return None
        
        models = self.get_models_detailed_list(api_type)
        input_lower = model_name_input.lower().strip()
        
        exact_matches = []
        partial_matches = []
        
        for model in models:
            filename_lower = model["filename"].lower()
            name_lower = model["name"].lower()
            alias_lower = model["alias"].lower()
            
            if input_lower == filename_lower or input_lower == name_lower or (alias_lower and input_lower == alias_lower):
                exact_matches.append(model)
            elif input_lower in filename_lower or input_lower in name_lower or (alias_lower and input_lower in alias_lower):
                partial_matches.append(model)
        
        if exact_matches:
            return exact_matches[0]["index"]
        elif partial_matches:
            best_match = max(partial_matches, key=lambda m: (
                len(m["name"]) if input_lower in m["name"] else 0,
                len(m["alias"]) if m["alias"] and input_lower in m["alias"] else 0
            ))
            return best_match["index"]
        
        return None

    async def _update_models_from_api(self, api_type="rvc"):
        base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
        client = Client(base_url)
        try:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")
        except Exception:
            model_list_from_api = await self._async_predict(client, api_name="/show_model")

        if not isinstance(model_list_from_api, list):
            raise ValueError(f"获取模型列表失败: {model_list_from_api}")

        current_config_list = self.svc_models_keywords if api_type == "svc" else self.rvc_models_keywords
        old_aliases = {}
        for item_str in current_config_list:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [f"{m}{MODEL_ALIAS_SEPARATOR}{old_aliases.get(m, '')}" for m in model_list_from_api]
        
        if api_type == "svc":
            self.svc_models_keywords = new_models_list
            self.config["svc_models_keywords"] = new_models_list
        else:
            self.rvc_models_keywords = new_models_list
            self.config["rvc_models_keywords"] = new_models_list
        
        self.config.save_config()
        logger.info(f"{api_type.upper()} 模型列表已更新并成功保存，共 {len(new_models_list)} 个模型")

    # ==================== 命令处理（支持LLM强制模式） ====================
    
    @filter.command("刷新rvc模型")
    async def refresh_rvc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 RVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="rvc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="rvc")
            display_str = display_str or "未发现任何模型。"
            chain=[Plain(f"当前 RVC 可用模型：\n{display_str}")]
            node = Node(uin=1109587454, name="松子", content=chain)
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 RVC 模型出错了: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置rvc后端链接")
    async def set_rvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置rvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 RVC 后端: {self.rvc_base_url}\n用法: /设置rvc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.rvc_base_url = _url
        self.config["rvc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"RVC 后端链接已设置为: {_url}")

    @filter.command("rvc")
    async def rvc(self, event: AstrMessageEvent):
        if not self.enable_rvc:
            yield event.plain_result("❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。")
            return
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/rvc 命令已被禁用。\n请直接对我说'翻唱《歌名》'，我会自动帮您处理！")
            return
        if self.disable_netease:
            yield event.plain_result("网易云音乐点歌功能已被禁用。\n请使用 /qqrvc <歌名> 使用QQ音乐点歌翻唱！")
            return
        async for result in self._handle_cover(event, api_type="rvc"):
            yield result

    @filter.command("刷新svc模型")
    async def refresh_svc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 SVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="svc")
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list(api_type="svc")
            display_str = display_str or "未发现任何模型。"
            chain=[Plain(f"当前 SVC 可用模型：\n{display_str}")]
            node = Node(uin=1109587454, name="松子", content=chain)
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 SVC 模型出错了: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置svc后端链接")
    async def set_svc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置svc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 SVC 后端: {self.svc_base_url}\n用法: /设置svc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.svc_base_url = _url
        self.config["svc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"SVC 后端链接已设置为: {_url}")

    @filter.command("svc")
    async def svc(self, event: AstrMessageEvent):
        if not self.enable_svc:
            yield event.plain_result("❌ SVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_svc 为 true。")
            return
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/svc 命令已被禁用。\n请直接对我说'翻唱《歌名》'，我会自动帮您处理！")
            return
        if self.disable_netease:
            yield event.plain_result("网易云音乐点歌功能已被禁用。\n请使用 /qqrvc <歌名> 或 /qqsvc <歌名> 使用QQ音乐点歌翻唱！")
            return
        async for result in self._handle_cover(event, api_type="svc"):
            yield result

    @filter.command("qqrvc")
    async def qqrvc(self, event: AstrMessageEvent):
        if not self.enable_rvc:
            yield event.plain_result("❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。")
            return
        if not self.enable_qqmusic:
            yield event.plain_result("❌ QQ音乐功能未启用！请在插件配置中开启 'enable_qqmusic' 开关。")
            return
        if not QQ_MUSIC_AVAILABLE:
            yield event.plain_result("❌ qqmusic-api-python 库未安装！请运行: pip install qqmusic-api-python")
            return
        async for result in self._handle_qq_cover(event, api_type="rvc"):
            yield result

    @filter.command("qqsvc")
    async def qqsvc(self, event: AstrMessageEvent):
        if not self.enable_svc:
            yield event.plain_result("❌ SVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_svc 为 true。")
            return
        if not self.enable_qqmusic:
            yield event.plain_result("❌ QQ音乐功能未启用！请在插件配置中开启 'enable_qqmusic' 开关。")
            return
        if not QQ_MUSIC_AVAILABLE:
            yield event.plain_result("❌ qqmusic-api-python 库未安装！请运行: pip install qqmusic-api-python")
            return
        async for result in self._handle_qq_cover(event, api_type="svc"):
            yield result

    @filter.command("qq点歌")
    async def qq_search(self, event: AstrMessageEvent):
        if not self.enable_qqmusic:
            yield event.plain_result("❌ QQ音乐功能未启用！请在插件配置中开启 'enable_qqmusic' 开关。")
            return
        if not QQ_MUSIC_AVAILABLE:
            yield event.plain_result("❌ qqmusic-api-python 库未安装！请运行: pip install qqmusic-api-python")
            return
        keyword = event.message_str.replace("qq点歌", "").strip()
        if not keyword:
            yield event.plain_result("用法: /qq点歌 <关键词>")
            return
        try:
            from .api import QQMusicAPI
            api = QQMusicAPI()
            songs = await api.fetch_data(keyword=keyword, limit=5)
            await api.close()
            if not songs:
                yield event.plain_result(f"在QQ音乐未找到与 '{keyword}' 相关的歌曲。")
                return
            lines = [f"🎵 在QQ音乐找到 {len(songs)} 首相关歌曲：", ""]
            for i, song in enumerate(songs, 1):
                duration_sec = song.get("duration", 0) // 1000
                minutes, seconds = divmod(duration_sec, 60)
                lines.append(f"{i}. {song['name']} - {song['artists']} ({minutes}:{seconds:02d})")
            lines.append("")
            lines.append("💡 使用 /qqrvc <序号> 或 /qqsvc <序号> 进行翻唱")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"QQ音乐搜索失败: {e}")
            yield event.plain_result(f"搜索失败: {e}")

    async def _handle_cover(self, event: AstrMessageEvent, api_type="rvc"):
        cmd = api_type
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调]")
            return

        key_shift, song_name = 0, " ".join(args)
        if args and args[-1].lstrip('-').isdigit():
            try:
                val = int(args[-1])
                if -12 <= val <= 12:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError: pass
        
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        songs = await self.api.fetch_data(keyword=song_name, limit=10)
        if not songs:
            yield event.plain_result("没能找到这首歌~")
            return
        
        await self._send_selection(event, songs)
        yield event.plain_result(f"请在{self.timeout}秒内输入歌曲序号进行选择：")
        
        selected_song_index = None
        id = event.get_sender_id()
        
        @session_waiter(timeout=self.timeout)
        async def song_waiter(controller: SessionController, event: AstrMessageEvent):
            if event.get_sender_id() != id:
                return            
            nonlocal selected_song_index
            user_input = event.message_str.strip()
            if user_input.isdigit() and 1 <= int(user_input) <= len(songs):
                selected_song_index = int(user_input) - 1
                controller.stop()

        try:
            await song_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return
        
        if selected_song_index is None:
             return
             
        selected_song = songs[selected_song_index]

        display_str, keys = self.get_models_display_list(api_type=api_type)
        if not keys:
            yield event.plain_result(f"当前没有可用的 {api_type.upper()} 模型，请先使用 /刷新{api_type}模型。")
            return
        
        chain=[Plain(f"已选歌曲: {selected_song['name']}\n使用: {api_type.upper()}\n\n可用模型：\n{display_str}")]
        node = Node(uin=1109587454, name="松子", content=chain)
        await event.send(event.chain_result([node]))
        yield event.plain_result(f"请在{self.timeout}秒内输入模型序号：")
        
        selected_model_index = None

        @session_waiter(timeout=self.timeout)
        async def model_waiter(controller: SessionController, event: AstrMessageEvent):
            if event.get_sender_id() != id:
                return    
            nonlocal selected_model_index
            user_input = event.message_str.strip()
            if user_input.isdigit() and 1 <= int(user_input) <= len(keys):
                selected_model_index = int(user_input) - 1
                controller.stop()

        try:
            await model_waiter(event)
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return

        if selected_model_index is None:
             return

        selected_model = keys[selected_model_index]

        yield event.plain_result(f"正在使用 {api_type.upper()} 模型【{selected_model}】为您生成《{selected_song['name']}》，请耐心等待...")
        await self._send_song(event=event, song=selected_song, model_name=selected_model, key_shift=key_shift, api_type=api_type)

    async def _handle_qq_cover(self, event: AstrMessageEvent, api_type="rvc"):
        cmd = "qqrvc" if api_type == "rvc" else "qqsvc"
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调]")
            return

        key_shift, song_name = 0, " ".join(args)
        if args and args[-1].lstrip('-').isdigit():
            try:
                val = int(args[-1])
                if -12 <= val <= 12:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError: pass
        
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        from .api import QQMusicAPI
        qq_api = QQMusicAPI()
        try:
            songs = await qq_api.fetch_data(keyword=song_name, limit=10)
            if not songs:
                yield event.plain_result("在QQ音乐没能找到这首歌~")
                return
            
            yield event.plain_result(f"🎵 QQ音乐搜索结果：")
            await self._send_selection(event, songs)
            yield event.plain_result(f"请在{self.timeout}秒内输入歌曲序号进行选择：")
            
            selected_song_index = None
            id = event.get_sender_id()
            
            @session_waiter(timeout=self.timeout)
            async def song_waiter(controller: SessionController, event: AstrMessageEvent):
                if event.get_sender_id() != id:
                    return            
                nonlocal selected_song_index
                user_input = event.message_str.strip()
                if user_input.isdigit() and 1 <= int(user_input) <= len(songs):
                    selected_song_index = int(user_input) - 1
                    controller.stop()

            try:
                await song_waiter(event)
            except TimeoutError:
                yield event.plain_result("选择超时，操作已取消。")
                return
            
            if selected_song_index is None:
                 return
                 
            selected_song = songs[selected_song_index]

            display_str, keys = self.get_models_display_list(api_type=api_type)
            if not keys:
                yield event.plain_result(f"当前没有可用的 {api_type.upper()} 模型，请先使用 /刷新{api_type}模型。")
                return
            
            chain=[Plain(f"[QQ音乐] 已选歌曲: {selected_song['name']}\n使用: {api_type.upper()}\n\n可用模型：\n{display_str}")]
            node = Node(uin=1109587454, name="松子", content=chain)
            await event.send(event.chain_result([node]))
            yield event.plain_result(f"请在{self.timeout}秒内输入模型序号：")
            
            selected_model_index = None

            @session_waiter(timeout=self.timeout)
            async def model_waiter(controller: SessionController, event: AstrMessageEvent):
                if event.get_sender_id() != id:
                    return    
                nonlocal selected_model_index
                user_input = event.message_str.strip()
                if user_input.isdigit() and 1 <= int(user_input) <= len(keys):
                    selected_model_index = int(user_input) - 1
                    controller.stop()

            try:
                await model_waiter(event)
            except TimeoutError:
                yield event.plain_result("选择超时，操作已取消。")
                return

            if selected_model_index is None:
                 return

            selected_model = keys[selected_model_index]

            yield event.plain_result(f"🎵 正在使用 {api_type.upper()} 模型【{selected_model}】为您生成《{selected_song['name']}》（QQ音乐），请耐心等待...")
            await self._send_song(event=event, song=selected_song, model_name=selected_model, key_shift=key_shift, api_type=api_type)
        finally:
            await qq_api.close()

    async def _send_selection(self, event: AstrMessageEvent, songs: list):
        formatted_songs = [f"{i + 1}. {s['name']} - {s['artists']}" for i, s in enumerate(songs[:10])]
        chain=[Plain("为您找到以下歌曲：\n" + "\n".join(formatted_songs))]
        node = Node(uin=1109587454, name="松子", content=chain)
        await event.send(event.chain_result([node]))

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: int, api_type="rvc"):
        result_path = None
        temp_audio_file = None
        song_name = song.get("name", "翻唱")
        try:
            base_url = self.svc_base_url if api_type == "svc" else self.rvc_base_url
            client = Client(base_url)
            
            # 判断是否为QQ音乐歌曲（通过songmid字段判断）
            is_qq_music = "songmid" in song and song.get("songmid")
            
            if is_qq_music:
                # QQ音乐：下载音频到本地临时文件（避免中文文件名问题）
                import aiohttp
                from .api import QQMusicAPI
                
                qq_api = QQMusicAPI()
                try:
                    songmid = song.get("songmid", "")
                    song_name = song.get("name", "")
                    audio_url = await qq_api.fetch_song_url(songmid, song_name=song_name)
                    
                    if not audio_url:
                        await event.send(event.plain_result("❌ 无法获取QQ音乐播放链接，请稍后重试。"))
                        return
                    
                    # 下载到本地临时文件（使用英文文件名）
                    temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
                    os.makedirs(temp_dir, exist_ok=True)
                    temp_audio_file = os.path.join(temp_dir, f"qq_{hash(songmid) % 10000000}.mp3")
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            if resp.status == 200:
                                with open(temp_audio_file, 'wb') as f:
                                    async for chunk in resp.content.iter_chunked(8192):
                                        f.write(chunk)
                            else:
                                await event.send(event.plain_result(f"❌ 下载QQ音乐失败 (HTTP {resp.status})"))
                                return
                    
                    # 使用本地文件路径
                    song_input = temp_audio_file
                finally:
                    await qq_api.close()
            else:
                # 网易云等平台：使用ID或名称
                song_input = song.get("name", str(song.get("id", "unknown")))
                if isinstance(song.get("id"), int) or (isinstance(song.get("id"), str) and song["id"].isdigit()):
                    song_input = str(song["id"])
            
            result_path = await self._async_predict(
                client,
                song_name_src=song_input,
                key_shift=key_shift,
                vocal_vol=0,
                inst_vol=0,
                model_dropdown=model_name,
                reverb_intensity=self.reverb_intensity,
                delay_intensity=self.delay_intensity,
                **({"svc_f0_method": self.svc_f0_method} if api_type == "svc" else {"f0_method": self.f0_method, "index_rate": self.index_rate, "filter_radius": self.filter_radius}),
                msst_batch_size=self.msst_batch_size,
                msst_num_overlap=self.msst_num_overlap,
                msst_normalize=self.msst_normalize,
                api_name="/convert",
                timeout=self.inference_timeout,
                event=event
            )
            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=song_name)
            else:
                await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                await event.send(event.plain_result(f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。如果需要，请在配置文件中调高 'inference_timeout' 的值。"))
            else:
                await event.send(event.plain_result(f"生成时发生严重错误: {e}"))
        finally:
            if self.enable_send_file:
                await asyncio.sleep(3)
            if result_path and os.path.isfile(result_path):
                try: os.remove(result_path)
                except OSError as e: logger.debug(f"删除临时文件失败（文件可能仍被占用）: {e}")
            if temp_audio_file and os.path.isfile(temp_audio_file):
                try: os.remove(temp_audio_file)
                except OSError as e: logger.error(f"删除临时音频文件失败: {e}")

    # ==================== 基础LLM工具（原有功能） ====================
    
    @filter.llm_tool(name="search_music")
    async def search_music(self, event: AstrMessageEvent, keyword: str, limit: int = 5, platform: str = "default") -> str:
        '''搜索音乐平台的歌曲。

        Args:
            keyword(string): 搜索关键词，可以是歌曲名、歌手名等
            limit(number): 返回结果数量上限，默认为5，最大为10
            platform(string, optional): 音乐平台选择，可选值：
                - 'default': 使用配置中设置的平台（默认）
                - 'netease' 或 '网易云': 网易云音乐
                - 'qq' 或 'qqmusic' 或 'QQ音乐': QQ音乐（需要启用QQ音乐功能）
        
        Returns:
            返回搜索到的歌曲列表，包含序号、歌名、歌手和歌曲ID
        '''
        try:
            limit = min(max(limit, 1), 10)
            
            # 确定使用的API
            api_to_use = self.api
            platform_name = "网易云音乐"
            
            if platform.lower() in ['qq', 'qqmusic', 'qq音乐', 'qq音乐']:
                if not self.enable_qqmusic:
                    return "❌ QQ音乐功能未启用！请在插件配置中开启 'enable_qqmusic' 开关。"
                if not QQ_MUSIC_AVAILABLE:
                    return "❌ qqmusic-api-python 库未安装！请联系管理员安装。"
                
                from .api import QQMusicAPI
                api_to_use = QQMusicAPI()
                platform_name = "QQ音乐"
            elif platform.lower() in ['netease', '网易云', 'netease_cloud', 'default']:
                if self.disable_netease:
                    return "❌ 网易云音乐功能已被禁用！请使用 platform='qq' 或 platform='qqmusic' 搜索QQ音乐。"
            
            songs = await api_to_use.fetch_data(keyword=keyword, limit=limit)
            
            if not songs:
                return f"在{platform_name}未找到与 '{keyword}' 相关的歌曲。"
            
            if self.enable_enhanced_context:
                result_lines = [
                    f"🎵 在{platform_name}找到 {len(songs)} 首相关歌曲：",
                    "",
                    "【歌曲列表】"
                ]
                for i, song in enumerate(songs, 1):
                    result_lines.append(
                        f"  {i}. 《{song['name']}》 - {song['artists']}"
                        f"\n     ID: {song['id']}"
                    )
                
                result_lines.extend([
                    "",
                    "💡 快捷指令提示：",
                    f"- 直接说 '翻唱第X首' 或 '用第Y个模型翻唱第X首'",
                    "- 我会自动帮您完成后续步骤！",
                    "",
                    f"📝 当前状态：已在{platform_name}搜索 '{keyword}'，共 {len(songs)} 首歌曲可选"
                ])
            else:
                result_lines = [f"🎵 在{platform_name}找到 {len(songs)} 首相关歌曲："]
                for i, song in enumerate(songs, 1):
                    result_lines.append(
                        f"{i}. 《{song['name']}》 - {song['artists']} (ID: {song['id']})"
                    )
                result_lines.append(f"\n请告诉用户搜索结果，并询问他们想要翻唱哪首歌（输入序号）。数据来源：{platform_name}")
            
            return "\n".join(result_lines)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"搜索歌曲时出错: {e}"

    @filter.llm_tool(name="rvc_cover")
    async def rvc_cover(self, event: AstrMessageEvent, song_name: str, artist_name: Optional[str] = None, model_index: int = 1, model_name: Optional[str] = None, key_shift: int = 0, music_source: Optional[str] = None) -> str:
        '''使用RVC模型翻唱歌曲。

        Args:
            song_name(string): 要翻唱的歌曲名称
            artist_name(string, optional): 歌手名称（如果有指定版本/歌手，请务必填入此参数，如'雨宫天'）
            model_index(number): 使用的模型序号（从可用模型列表中选择），默认为1表示第一个模型
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth
            key_shift(number): 音调调整，范围为-12到12，默认为0
            music_source(string, optional): 音乐源选择，'netease'(网易云) 或 'qqmusic'(QQ音乐)
                不指定则使用默认音乐源。推荐QQ音乐！
        
        Returns:
            翻唱结果的描述信息（异步模式会立即返回"正在处理"）
        
        注意：
            RVC功能可能已在配置中禁用，此时会返回错误提示。
        '''
        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true，或使用 svc_cover 工具进行SVC翻唱。"
        if model_name:
            matched = self._find_model_index_by_name(model_name, "rvc")
            if matched:
                model_index = matched
            else:
                return f"❌ 未找到匹配的RVC模型: '{model_name}'\n\n💡 提示：使用 get_available_models('rvc') 查看可用模型"
        
        models_info = self.get_models_detailed_list("rvc")
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break
        
        actual_music_source = music_source or self.default_api
        if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
            actual_music_source = "qqmusic"
        source_display = "QQ音乐" if actual_music_source == "qqmusic" else "网易云"
        
        search_query = f"{song_name} {artist_name}" if artist_name else song_name
        
        asyncio.create_task(self._smart_cover_async(
            event, search_query, "rvc", model_index, key_shift, model_display, actual_music_source
        ))
        return f"🎵 正在从【{source_display}】用RVC模型【{model_display}】翻唱《{song_name}》... 请稍等，音频生成后会自动发送！"

    @filter.llm_tool(name="svc_cover")
    async def svc_cover(self, event: AstrMessageEvent, song_name: str, artist_name: Optional[str] = None, model_index: int = 1, model_name: Optional[str] = None, key_shift: int = 0, music_source: Optional[str] = None) -> str:
        '''使用SVC模型翻唱歌曲。

        Args:
            song_name(string): 要翻唱的歌曲名称
            artist_name(string, optional): 歌手名称（如果有指定版本/歌手，请务必填入此参数，如'雨宫天'）
            model_index(number): 使用的模型序号（从可用模型列表中选择），默认为1表示第一个模型
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth
            key_shift(number): 音调调整，范围为-12到12，默认为0
            music_source(string, optional): 音乐源选择，'netease'(网易云) 或 'qqmusic'(QQ音乐)
                不指定则使用默认音乐源。推荐QQ音乐！
        
        Returns:
            翻唱结果的描述信息（异步模式会立即返回"正在处理"）
        
        注意：
            SVC功能可能已在配置中禁用，此时会返回错误提示。
        '''
        if not self.enable_svc:
            return "❌ SVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_svc 为 true，或使用 rvc_cover 工具进行RVC翻唱。"
        if model_name:
            matched = self._find_model_index_by_name(model_name, "svc")
            if matched:
                model_index = matched
            else:
                return f"❌ 未找到匹配的SVC模型: '{model_name}'\n\n💡 提示：使用 get_available_models('svc') 查看可用模型"
        
        models_info = self.get_models_detailed_list("svc")
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break
        
        actual_music_source = music_source or self.default_api
        if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
            actual_music_source = "qqmusic"
        source_display = "QQ音乐" if actual_music_source == "qqmusic" else "网易云"
        
        search_query = f"{song_name} {artist_name}" if artist_name else song_name
        
        asyncio.create_task(self._smart_cover_async(
            event, search_query, "svc", model_index, key_shift, model_display, actual_music_source
        ))
        return f"🎵 正在从【{source_display}】用SVC模型【{model_display}】翻唱《{song_name}》... 请稍等，音频生成后会自动发送！"

    async def _do_cover(self, event: AstrMessageEvent, song_name: str, api_type: str, model_index: int, key_shift: int, music_source: str = None) -> str:
        """统一的翻唱执行逻辑"""
        try:
            models_keywords = self.rvc_models_keywords if api_type == "rvc" else self.svc_models_keywords
            
            if not models_keywords:
                return f"当前没有可用的 {api_type.upper()} 模型，请先使用 /刷新{api_type}模型 命令更新模型列表。"
            
            if not (1 <= model_index <= len(models_keywords)):
                return f"模型序号无效，当前共有 {len(models_keywords)} 个模型可选，请输入 1 到 {len(models_keywords)} 之间的数字。"
            
            if not (-12 <= key_shift <= 12):
                return "音调调整值无效，请在 -12 到 12 之间选择。"
            
            # === 根据音乐源选择 API ===
            actual_music_source = music_source or self.default_api
            if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
                actual_music_source = "qqmusic"
            if actual_music_source == "qqmusic":
                if not self.enable_qqmusic:
                    return "❌ QQ音乐功能未启用"
                from .api import QQMusicAPI
                search_api = QQMusicAPI()
            elif actual_music_source in ["netease", "netease_nodejs"]:
                if self.disable_netease:
                    return "❌ 网易云音乐功能已禁用"
                search_api = self.api
            else:
                search_api = self.api
            
            songs = await search_api.fetch_data(keyword=song_name, limit=10)
            if not songs:
                return f"未找到歌曲 '{song_name}'。"
            
            # 智能匹配：如果搜索结果有多个，尝试找到和用户搜索词最匹配的一首
            selected_song = songs[0]
            if len(songs) > 1:
                from difflib import SequenceMatcher
                best_score = -1
                
                # 预处理搜索词：将繁体/特殊字符做简单归一化
                normalized_search = song_name.lower().replace("宫", "宮")
                search_keywords = normalized_search.replace("-", " ").replace("、", " ").split()
                
                for song in songs:
                    song_title = song.get('name', '').lower()
                    artist_name = song.get('artists', '').lower()
                    song_info = f"{song_title} {artist_name}"
                    
                    # 1. 基础相似度
                    score = SequenceMatcher(None, normalized_search, song_info).ratio()
                    
                    # 2. 关键词命中加分
                    if len(search_keywords) > 0:
                        match_count = sum(1 for kw in search_keywords if kw in song_info)
                        score += (match_count / len(search_keywords)) * 1.5
                        
                    # 3. 歌名直接命中加分（非常重要，防止只匹配到歌手却选错歌）
                    if song_title and (song_title in normalized_search or normalized_search in song_title):
                        score += 2.0
                        
                    # 4. 歌手直接命中加分
                    if artist_name and (artist_name in normalized_search or normalized_search in artist_name):
                        score += 1.0
                        
                    if score > best_score:
                        best_score = score
                        selected_song = song

            _, keys = self.get_models_display_list(api_type=api_type)
            selected_model = keys[model_index - 1]
            
            # === QQ音乐：先下载音频到本地文件 ===
            song_input_for_api = str(selected_song["id"])
            temp_audio_file = None
            
            if actual_music_source == "qqmusic":
                try:
                    from .api import QQMusicAPI
                    qq_api = QQMusicAPI()
                    songmid = selected_song.get("songmid") or selected_song["id"]
                    song_display_name = selected_song.get("name", song_name)
                    
                    logger.info(f"QQ音乐: 获取播放链接 {songmid} - {song_display_name}")
                    audio_url = await qq_api.fetch_song_url(songmid, song_display_name)
                    
                    if not audio_url:
                        return f"❌ 无法获取QQ音乐播放链接: {song_display_name}"
                    
                    import aiohttp
                    temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
                    os.makedirs(temp_dir, exist_ok=True)
                    temp_audio_file = os.path.join(temp_dir, f"qq_{hash(songmid) % 10000000}.mp3")
                    
                    logger.info(f"QQ音乐: 下载音频到 {temp_audio_file}")
                    async with aiohttp.ClientSession() as session:
                        async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            if resp.status == 200:
                                with open(temp_audio_file, "wb") as f:
                                    async for chunk in resp.content.iter_chunked(8192):
                                        f.write(chunk)
                                logger.info(f"QQ音乐: 下载完成，文件大小: {os.path.getsize(temp_audio_file)} bytes")
                            else:
                                return f"❌ QQ音乐下载失败: HTTP {resp.status}"
                    
                    song_input_for_api = temp_audio_file
                    
                except Exception as e:
                    logger.error(f"QQ音乐下载失败: {traceback.format_exc()}")
                    return f"❌ QQ音乐处理失败: {e}"
            
            base_url = self.rvc_base_url if api_type == "rvc" else self.svc_base_url
            client = Client(base_url)
            result_path = await self._async_predict(
                client,
                song_name_src=song_input_for_api,
                key_shift=key_shift,
                vocal_vol=0,
                inst_vol=0,
                model_dropdown=selected_model,
                reverb_intensity=self.reverb_intensity,
                delay_intensity=self.delay_intensity,
                **({"svc_f0_method": self.svc_f0_method} if api_type == "svc" else {"f0_method": self.f0_method, "index_rate": self.index_rate, "filter_radius": self.filter_radius}),
                msst_batch_size=self.msst_batch_size,
                msst_num_overlap=self.msst_num_overlap,
                msst_normalize=self.msst_normalize,
                api_name="/convert",
                timeout=self.inference_timeout,
                event=event
            )
            
            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=selected_song.get('name', song_name))
                song_artist = selected_song.get('artists', '未知艺人')
                result_msg = f"已成功使用 {api_type.upper()} 模型【{selected_model}】生成《{selected_song['name']}》({song_artist}) 的翻唱版本！音频文件已发送。"
            else:
                result_msg = "生成失败，后端未返回有效文件路径。"
                
            try:
                if self.enable_send_file:
                    await asyncio.sleep(3)
                if result_path and os.path.isfile(result_path):
                    os.remove(result_path)
                if temp_audio_file and os.path.isfile(temp_audio_file):
                    os.remove(temp_audio_file)
            except OSError as e:
                logger.debug(f"删除临时文件失败（文件可能仍被占用，将在退出时清理）: {e}")
            
            # === 方案E：记录偏好 ===
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                pref["usage_count"] += 1
                pref["last_used_time"] = datetime.now().isoformat()
                pref["last_used_model"] = selected_model
                pref["last_used_api_type"] = api_type
                
                if selected_song['name'] not in pref["favorite_songs"]:
                    pref["favorite_songs"].append(selected_song['name'])
                    if len(pref["favorite_songs"]) > 20:
                        pref["favorite_songs"] = pref["favorite_songs"][-20:]
                
                artist = selected_song['artists'].split(',')[0].strip() if ',' in selected_song['artists'] else selected_song['artists']
                if artist not in pref["preferred_artists"]:
                    pref["preferred_artists"].append(artist)
                    if len(pref["preferred_artists"]) > 10:
                        pref["preferred_artists"] = pref["preferred_artists"][-10:]
                
                self._save_preferences()
            
            return result_msg
                
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                return f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。"
            else:
                return f"{api_type.upper()} 翻唱时发生错误: {e}"

    @filter.llm_tool(name="get_available_models")
    async def get_available_models(self, event: AstrMessageEvent, api_type: str = "all") -> str:
        '''获取当前可用的RVC/SVC模型列表（包含文件名和别名，支持模糊匹配）。

        Args:
            api_type(string): 要查看的模型类型，'rvc'、'svc' 或 'all'(显示全部)，默认为'all'
        
        Returns:
            可用模型的详细信息列表（包含序号、显示名、文件名、别名）
        
        重要提示：
            - 可以直接用模型名称或别名指定模型，无需记住序号
            - 例如：model_name='塔菲' 会自动匹配到 tafeim.pth
            - 支持模糊匹配：输入部分名称也能找到
            - 只会显示已启用的引擎（enable_rvc / enable_svc）
        '''
        try:
            result_parts = []
            
            if api_type.lower() in ["all", "rvc"] and self.enable_rvc:
                rvc_models = self.get_models_detailed_list(api_type="rvc")
                if rvc_models:
                    lines = []
                    for m in rvc_models:
                        alias_info = f" (别名: {m['alias']})" if m['alias'] else ""
                        lines.append(f"  {m['index']}. {m['display']}【{m['name']}】{alias_info}")
                    result_parts.append(f"【RVC 模型】(共 {len(rvc_models)} 个)\n" + "\n".join(lines))
                else:
                    result_parts.append("【RVC 模型】暂无可用模型，请使用 /刷新rvc模型")
            
            if api_type.lower() in ["all", "svc"] and self.enable_svc:
                svc_models = self.get_models_detailed_list(api_type="svc")
                if svc_models:
                    lines = []
                    for m in svc_models:
                        alias_info = f" (别名: {m['alias']})" if m['alias'] else ""
                        lines.append(f"  {m['index']}. {m['display']}【{m['name']}】{alias_info}")
                    result_parts.append(f"\n【SVC 模型】(共 {len(svc_models)} 个)\n" + "\n".join(lines))
                else:
                    result_parts.append("\n【SVC 模型】暂无可用模型，请使用 /刷新svc模型")
            
            if not result_parts:
                return "没有可用的模型。"
            
            result_parts.append("\n\n💡 使用方式：")
            result_parts.append("- 用序号: model_index=2 （第2个模型）")
            result_parts.append("- 用名称: model_name='tafeim' 或 model_name='塔菲' （自动匹配）")
            result_parts.append("- 示例: '用塔菲模型翻唱《晴天》'")
            
            status_parts = []
            if self.enable_rvc:
                status_parts.append("RVC ✅")
            else:
                status_parts.append("RVC ❌(已禁用)")
            if self.enable_svc:
                status_parts.append("SVC ✅")
            else:
                status_parts.append("SVC ❌(已禁用)")
            result_parts.append(f"\n🔧 引擎状态: {' | '.join(status_parts)}")
            
            return "\n".join(result_parts)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取模型列表时出错: {e}"

    # ==================== 方案A：智能单步翻唱 ====================
    
    @filter.llm_tool(name="smart_cover")
    async def smart_cover(self, event: AstrMessageEvent, 
                         song_name: str, 
                         artist_name: Optional[str] = None,
                         api_type: Optional[str] = None,
                         model_index: Optional[int] = None,
                         model_name: Optional[str] = None,
                         key_shift: Optional[int] = None,
                         music_source: Optional[str] = None) -> str:
        '''智能一键翻唱工具 - 自动完成搜索+选择+翻唱全流程！

        这是最高效的翻唱方式，只需提供歌曲名即可自动完成所有步骤。
        
        Args:
            song_name(string): 要翻唱的歌曲名称（必填）
            artist_name(string, optional): 歌手名称（如果有指定版本/歌手，请务必填入此参数，如'雨宫天'）
            api_type(string, optional): 翻唱类型，'rvc' 或 'svc'。不指定则使用默认值或用户偏好
                当前可用引擎: {available_engines}
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认值或用户偏好
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth，'maxmaxpoi'也能匹配
            key_shift(number, optional): 音调调整(-12到12)。不指定则使用默认值或用户偏好
            music_source(string, optional): 音乐源选择，'netease'(网易云) 或 'qqmusic'(QQ音乐)。
                不指定则使用配置的默认音乐源。推荐QQ音乐，版权更全！
                示例：music_source='qqmusic'
        
        Returns:
            翻唱结果描述（异步模式会立即返回"正在处理"）
        
        注意：
            - model_name 支持中文别名和文件名，无需记住序号！
            - 如果同时指定 model_index 和 model_name，以 model_name 为准
            - 本工具采用异步执行，会立即返回并后台处理，完成后自动发送音频
            - 推荐使用 music_source='qqmusic' 获取更多正版音乐
            - 如果指定的 api_type 已被禁用，会自动切换到可用的引擎或返回提示
        '''
        if not self.enable_smart_cover:
            return "智能单步翻唱功能已在配置中禁用。请使用 search_music + rvc_cover/svc_cover 组合。"
        
        try:
            available_engines = []
            if self.enable_rvc:
                available_engines.append("rvc")
            if self.enable_svc:
                available_engines.append("svc")
            
            if not available_engines:
                return "❌ RVC 和 SVC 功能都已在配置中禁用！请在插件配置中至少启用一个引擎（enable_rvc 或 enable_svc）。"
            
            actual_api_type = api_type or self.default_api_type
            
            if actual_api_type and actual_api_type.lower() not in available_engines:
                fallback = available_engines[0]
                return f"⚠️ {actual_api_type.upper()} 引擎已在配置中禁用。\n当前可用的翻唱引擎: {', '.join(available_engines).upper()}\n请指定可用的引擎，例如 api_type='{fallback}'"
            
            if model_name:
                matched_index = self._find_model_index_by_name(model_name, actual_api_type)
                if matched_index:
                    actual_model_index = matched_index
                else:
                    available = await self.get_available_models(event, actual_api_type)
                    return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用的模型：\n{available}\n\n💡 提示：可以使用 get_available_models 查看完整列表"
            else:
                actual_model_index = model_index or self.default_model_index
            
            actual_key_shift = key_shift if key_shift is not None else self.default_key_shift
            
            actual_music_source = music_source or self.default_api
            if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
                actual_music_source = "qqmusic"
            if actual_music_source == "qqmusic" and not self.enable_qqmusic:
                return "❌ QQ音乐功能未启用，请在插件配置中开启 enable_qqmusic"
            
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                
                if not api_type and pref.get("default_api_type"):
                    actual_api_type = pref["default_api_type"]
                
                if not model_name and not model_index and pref.get("default_model_index"):
                    actual_model_index = pref["default_model_index"]
                
                if key_shift is None and pref.get("default_key_shift") is not None:
                    actual_key_shift = pref["default_key_shift"]
            
            models_info = self.get_models_detailed_list(actual_api_type)
            actual_model_display = ""
            for m in models_info:
                if m["index"] == actual_model_index:
                    actual_model_display = m["display"]
                    break
            
            source_display = "QQ音乐" if actual_music_source == "qqmusic" else ("网易云" if actual_music_source in ["netease", "netease_nodejs"] else actual_music_source)
            
            search_query = f"{song_name} {artist_name}" if artist_name else song_name
            
            asyncio.create_task(self._smart_cover_async(
                event, search_query, actual_api_type, actual_model_index, 
                actual_key_shift, actual_model_display, actual_music_source
            ))
            
            return f"🎵 正在从【{source_display}】用【{actual_model_display}】翻唱《{song_name}》... 请稍等，音频生成后会自动发送！"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"智能翻唱出错: {e}"

    async def _smart_cover_async(self, event: AstrMessageEvent, song_name: str, 
                                 api_type: str, model_index: int, key_shift: int,
                                 model_display: str, music_source: str = None):
        """smart_cover 异步执行函数"""
        try:
            result = await self._do_cover(event, song_name, api_type, model_index, key_shift, music_source)
            
            if self.enable_config_report:
                source_info = f", 音乐源={music_source}" if music_source else ""
                result += f"\n\n📊 本次配置：类型={api_type.upper()}, 模型={model_display}, 调音={key_shift:+d}{source_info}"
                await event.send(event.plain_result(result))
            
            # === LLM 成功通知机制 ===
            if getattr(self, "enable_llm_success_notify", True) and "已成功" in result:
                try:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider:
                        prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）已经由后台处理完成，并且音频已经发送给用户了！请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个好消息（不要包含系统通知字样）。"
                        llm_resp = await provider.text_chat(prompt=prompt)
                        if llm_resp and llm_resp.completion_text:
                            await event.send(event.plain_result(llm_resp.completion_text))
                except Exception as llm_e:
                    logger.error(f"通知 LLM 失败: {llm_e}")
            
        except Exception as e:
            logger.error(f"smart_cover 异步执行失败: {traceback.format_exc()}")
            try:
                await event.send(event.plain_result(f"❌ 翻唱失败: {e}"))
            except:
                pass

    # ==================== 方案C：确认机制 ====================
    
    @filter.llm_tool(name="confirm_selection")
    async def confirm_selection(self, event: AstrMessageEvent,
                               selection_type: str,
                               selection_data: str,
                               action: str = "confirm") -> str:
        '''确认或修改用户的 selections - 支持自然语言交互！

        当用户说'就这个''换一个''确定''取消'等自然语言时使用此工具。
        
        Args:
            selection_type(string): 选择类型，'song'(歌曲)、'model'(模型)、'config'(配置)
            selection_data(string): 当前选择的描述信息，如'歌曲：《晴天》-周杰伦'或'模型：第3个-RVC'
            action(string): 用户意图，'confirm'(确认)、'change'(更换)、'cancel'(取消)、'info'(查看详情)
        
        Returns:
            确认结果和下一步建议
        '''
        if not self.enable_confirm_mechanism:
            return "确认机制已在配置中禁用。"
        
        try:
            responses = {
                "confirm": {
                    "song": f"✅ 已确认选择：{selection_data}\n接下来将执行翻唱操作。",
                    "model": f"✅ 已确认使用：{selection_data}",
                    "config": f"✅ 配置已确认：{selection_data}\n准备开始翻唱..."
                },
                "change": {
                    "song": f"🔄 需要更换歌曲选择。\n当前选择：{selection_data}\n请重新搜索或指定新的歌曲。",
                    "model": f"🔄 需要更换模型。\n当前选择：{selection_data}\n可用模型列表：\n{(await self.get_available_models(event))}",
                    "config": f"🔄 需要修改配置。\n当前配置：{selection_data}\n请告诉我新的参数值。"
                },
                "cancel": {
                    "song": "❌ 已取消歌曲选择。",
                    "model": "❌ 已取消模型选择。",
                    "config": "❌ 已取消配置，操作终止。"
                },
                "info": {
                    "song": f"📋 歌曲详情：{selection_data}",
                    "model": f"📋 模型详情：{selection_data}",
                    "config": f"⚙️ 当前配置详情：{selection_data}"
                }
            }
            
            action_lower = action.lower() if action else "confirm"
            type_lower = selection_type.lower() if selection_type else "song"
            
            if action_lower not in responses:
                return f"未知操作: {action}。支持的选项: confirm, change, cancel, info"
            
            if type_lower not in responses[action_lower]:
                return f"未知选择类型: {selection_type}。支持的类型: song, model, config"
            
            result = responses[action_lower][type_lower]
            
            if action_lower == "confirm" and type_lower == "song":
                result += "\n\n💡 提示：您也可以直接说'就用这个翻唱'，我会自动继续！"
            
            return result
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"确认操作出错: {e}"

    # ==================== 方案D：批量翻唱 ====================
    
    @filter.llm_tool(name="batch_cover")
    async def batch_cover(self, event: AstrMessageEvent,
                         songs: List[str],
                         api_type: Optional[str] = None,
                         model_index: Optional[int] = None,
                         key_shift: Optional[int] = None,
                         music_source: Optional[str] = None) -> str:
        '''批量翻唱多首歌曲 - 异步后台执行，支持长时间任务！

        适合说'帮我翻唱这几首歌：《A》《B》《C》'的场景。
        
        重要特性：
            - 异步执行：立即返回确认消息，后台处理翻唱
            - 实时推送：每完成一首歌立即发送音频文件
            - 超时安全：不受60秒工具超时限制
        
        Args:
            songs(list[string]): 要翻唱的歌曲名称列表（必填）
            api_type(string, optional): 翻唱类型，'rvc' 或 'svc'
            model_index(number, optional): 统一使用的模型序号
            key_shift(number, optional): 统一的音调调整值
            music_source(string, optional): 音乐源选择，'netease'(网易云) 或 'qqmusic'(QQ音乐)
        
        Returns:
            任务确认信息（实际结果会异步推送到聊天中）
        '''
        if not self.enable_batch_cover:
            return "批量翻唱功能已在配置中禁用。请逐个使用 smart_cover 或 rvc_cover/svc_cover。"
        
        available_engines = []
        if self.enable_rvc:
            available_engines.append("rvc")
        if self.enable_svc:
            available_engines.append("svc")
        
        if not available_engines:
            return "❌ RVC 和 SVC 功能都已在配置中禁用！请在插件配置中至少启用一个引擎（enable_rvc 或 enable_svc）。"
        
        try:
            if not songs or len(songs) == 0:
                return "歌曲列表为空，请提供至少一首歌名。"
            
            if len(songs) > self.max_batch_size:
                return f"批量翻唱数量超过限制（最大{self.max_batch_size}首）。当前提供了{len(songs)}首，请分批处理。"
            
            # === 参数解析 ===
            actual_api_type = api_type or self.default_api_type
            
            if actual_api_type and actual_api_type.lower() not in available_engines:
                return f"⚠️ {actual_api_type.upper()} 引擎已在配置中禁用。\n当前可用的翻唱引擎: {', '.join(available_engines).upper()}\n请指定可用的引擎，例如 api_type='{available_engines[0]}'"
            
            actual_model_index = model_index or self.default_model_index
            actual_key_shift = key_shift if key_shift is not None else self.default_key_shift
            
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                if not api_type:
                    actual_api_type = pref.get("default_api_type", actual_api_type)
                if not model_index:
                    actual_model_index = pref.get("default_model_index", actual_model_index)
                if key_shift is None:
                    actual_key_shift = pref.get("default_key_shift", actual_key_shift)
            
            actual_music_source = music_source or self.default_api
            if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
                actual_music_source = "qqmusic"
                
            # === 创建后台任务（异步执行）===
            asyncio.create_task(
                self._execute_batch_cover_async(
                    event=event,
                    songs=songs,
                    api_type=actual_api_type,
                    model_index=actual_model_index,
                    key_shift=actual_key_shift,
                    music_source=actual_music_source
                )
            )
            
            # === 立即返回确认消息（不会被超时取消）===
            confirm_msg = (
                f"📦 已接收批量翻唱任务！共 {len(songs)} 首歌曲\n"
                f"⚙️ 配置：{actual_api_type.upper()} / 第{actual_model_index}个模型 / 音调{actual_key_shift:+d}\n"
                f"⏳ 正在后台处理中，请稍候...\n"
                f"💡 每完成一首会自动发送音频，最后发送汇总报告"
            )
            
            return confirm_msg
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"批量翻唱启动失败: {e}"

    async def _execute_batch_cover_async(self, event: AstrMessageEvent,
                                        songs: List[str],
                                        api_type: str,
                                        model_index: int,
                                        key_shift: int,
                                        music_source: str = None):
        """异步执行批量翻唱（后台任务）"""
        try:
            results = []
            success_count = 0
            fail_count = 0
            
            for idx, song_name in enumerate(songs, 1):
                try:
                    # 发送进度提示
                    progress_msg = f"🎵 [{idx}/{len(songs)}] 正在翻唱《{song_name}》..."
                    try:
                        await event.send(event.plain_result(progress_msg))
                    except Exception as e:
                        logger.warning(f"发送进度消息失败: {e}")
                    
                    # 执行翻唱
                    result = await self._do_cover(event, song_name, api_type, model_index, key_shift, music_source)
                    results.append(f"✅ [{idx}/{len(songs)}] 《{song_name}》：成功")
                    success_count += 1
                    
                    # 歌间延迟避免过快请求
                    if idx < len(songs):
                        await asyncio.sleep(2)
                        
                except asyncio.CancelledError:
                    results.append(f"❌ [{idx}/{len(songs)}] 《{song_name}》：被取消")
                    fail_count += 1
                    logger.warning(f"批量翻唱第{idx}首被取消")
                except Exception as e:
                    error_msg = str(e)[:80]
                    results.append(f"❌ [{idx}/{len(songs)}] 《{song_name}》：失败 - {error_msg}")
                    fail_count += 1
                    logger.error(f"批量翻唱第{idx}首出错: {e}")
            
            # === 生成并发送汇总报告 ===
            summary_lines = [
                "",
                "=" * 50,
                "📊 批量翻唱任务完成报告",
                "=" * 50,
                "",
                f"总任务数: {len(songs)} 首",
                f"✅ 成功: {success_count} 首",
                f"❌ 失败: {fail_count} 首",
                "",
                f"使用的配置:",
                f"  类型: {api_type.upper()}",
                f"  模型: 第{model_index}个",
                f"  音调: {key_shift:+d}",
                "",
                "-" * 50,
                "详细结果:",
            ]
            
            summary_lines.extend(results)
            
            summary_lines.extend([
                "",
                "=" * 50,
                f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 50
            ])
            
            final_report = "\n".join(summary_lines)
            
            # 发送汇总报告
            try:
                await event.send(event.plain_result(final_report))
            except Exception as e:
                logger.error(f"发送批量翻唱报告失败: {e}")
                
            # === LLM 成功通知机制 ===
            if getattr(self, "enable_llm_success_notify", True) and success_count > 0:
                try:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider:
                        prompt = f"系统通知：刚才用户要求的批量翻唱任务已经处理完成！共成功处理了 {success_count} 首歌曲。请用你当前的人设直接告诉用户这个好消息（不要包含系统通知字样）。"
                        llm_resp = await provider.text_chat(prompt=prompt)
                        if llm_resp and llm_resp.completion_text:
                            await event.send(event.plain_result(llm_resp.completion_text))
                except Exception as llm_e:
                    logger.error(f"通知 LLM 失败: {llm_e}")
                
        except Exception as e:
            logger.error(traceback.format_exc())
            error_report = f"❌ 批量翻唱过程发生严重错误: {str(e)[:100]}"
            try:
                await event.send(event.plain_result(error_report))
            except Exception:
                pass

    # ==================== 方案E：偏好学习与推荐系统 ====================
    
    @filter.llm_tool(name="save_preference")
    async def save_preference(self, event: AstrMessageEvent,
                             preference_type: str,
                             value: str) -> str:
        '''保存用户的偏好设置 - 让AI记住你的喜好！

        可以保存常用的模型、音调等设置，下次翻唱时自动应用。
        
        Args:
            preference_type(string): 偏好类型：
                - 'api_type': 默认翻唱类型 ('rvc' 或 'svc')
                - 'model_index': 默认模型序号 (数字字符串)
                - 'key_shift': 默认音调调整 (-12到12的数字字符串)
            value(string): 偏好值（统一使用字符串传入，系统会自动转换）
        
        Returns:
            保存结果确认
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            valid_types = {
                "api_type": {"type": str, "valid_values": ["rvc", "svc"]},
                "model_index": {"type": int, "min": 1},
                "key_shift": {"type": int, "min": -12, "max": 12}
            }
            
            if preference_type not in valid_types:
                return f"无效的偏好类型: {preference_type}\n支持的类型: {', '.join(valid_types.keys())}"
            
            type_info = valid_types[preference_type]
            target_type = type_info["type"]
            
            try:
                converted_value = target_type(value)
            except (ValueError, TypeError):
                return f"无法将值 '{value}' 转换为 {target_type.__name__} 类型"
            
            if "valid_values" in type_info and converted_value not in type_info["valid_values"]:
                return f"无效的值: {converted_value}，可选值: {type_info['valid_values']}"
            
            if "min" in type_info and converted_value < type_info["min"]:
                return f"值太小: {converted_value}，最小值为 {type_info['min']}"
            
            if "max" in type_info and converted_value > type_info["max"]:
                return f"值太大: {converted_value}，最大值为 {type_info['max']}"
            
            pref[f"default_{preference_type}"] = converted_value
            self._save_preferences()
            
            type_display = {
                "api_type": f"默认翻唱类型 → {'RVC' if converted_value == 'rvc' else 'SVC'}",
                "model_index": f"默认模型 → 第{converted_value}个",
                "key_shift": f"默认音调 → {converted_value:+d}"
            }
            
            return f"✅ 偏好已保存！\n{type_display.get(preference_type, preference_type)}\n\n下次翻唱时会自动使用此设置。如需修改，随时告诉我！"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"保存偏好时出错: {e}"

    @filter.llm_tool(name="get_recommendation")
    async def get_recommendation(self, event: AstrMessageEvent,
                                song_name: Optional[str] = None) -> str:
        '''基于历史记录智能推荐最佳翻唱配置！

        分析用户的历史使用习惯，推荐最合适的模型和参数。
        
        Args:
            song_name(string, optional): 计划翻唱的歌曲名（可选，用于结合歌曲特征推荐）
        
        Returns:
            推荐的配置方案和使用理由
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法提供个性化推荐。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            recommendation = []
            recommendation.append("🤖 基于您的使用历史，我为您推荐以下配置：\n")
            
            # === 基本信息 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "无")
            
            recommendation.append(f"📊 您的使用统计：")
            recommendation.append(f"   - 总翻唱次数: {usage_count}")
            recommendation.append(f"   - 最后使用时间: {last_time[:16] if last_time != '无' else last_time}")
            
            if usage_count == 0:
                recommendation.append(f"\n💡 您还没有使用过翻唱功能，将使用系统默认配置：")
                recommendation.append(f"   - 类型: {self.default_api_type.upper()}")
                recommendation.append(f"   - 模型: 第{self.default_model_index}个")
                recommendation.append(f"   - 音调: {self.default_key_shift:+d}")
                return "\n".join(recommendation)
            
            # === 推荐配置 ===
            rec_api_type = pref.get("default_api_type", self.default_api_type)
            rec_model_index = pref.get("default_model_index", self.default_model_index)
            rec_key_shift = pref.get("default_key_shift", self.default_key_shift)
            last_model = pref.get("last_used_model", "未知")
            
            recommendation.append(f"\n⭐ 推荐配置：")
            recommendation.append(f"   🎵 翻唱类型: {rec_api_type.upper()}")
            recommendation.append(f"   🎤 模型选择: 第{rec_model_index}个 ({last_model})")
            recommendation.append(f"   🎼 音调调整: {rec_key_shift:+d}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                recent_favorites = favorite_songs[-5:] if len(favorite_songs) > 5 else favorite_songs
                recommendation.append(f"\n❤️ 您最近常听的歌曲：")
                for song in recent_favorites:
                    recommendation.append(f"   - 《{song}》")
            
            # === 偏好歌手 ===
            preferred_artists = pref.get("preferred_artists", [])
            if preferred_artists:
                recommendation.append(f"\n🎤 您偏爱的歌手：")
                for artist in preferred_artists[-3:]:
                    recommendation.append(f"   - {artist}")
            
            # === 结合当前歌曲的建议 ===
            if song_name:
                recommendation.append(f"\n🎯 针对《{song_name}》的建议：")
                
                if any(artist in song_name for artist in preferred_artists):
                    recommendation.append(f"   ✨ 检测到这是您偏爱的歌手的作品！")
                    recommendation.append(f"   💡 建议使用您常用的配置进行翻唱。")
                
                if song_name in favorite_songs:
                    recommendation.append(f"   🔄 这首歌您之前翻唱过！")
                    recommendation.append(f"   💡 是否想尝试不同的模型或音调？")
            
            # === 使用建议 ===
            recommendation.append(f"\n💬 如何使用此推荐？")
            recommendation.append(f"   直接说：'翻唱《{song_name or "歌曲名"}》'")
            recommendation.append(f"   我会自动应用以上推荐的配置！")
            recommendation.append(f"\n⚙️ 如需修改偏好，可以说：")
            recommendation.append(f"   - '把默认模型改成第2个'")
            recommendation.append(f"   - '以后都用SVC翻唱'")
            recommendation.append(f"   - '默认升2调'")
            
            return "\n".join(recommendation)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取推荐时出错: {e}"

    @filter.llm_tool(name="view_my_stats")
    async def view_my_stats(self, event: AstrMessageEvent) -> str:
        '''查看个人使用统计和历史记录。

        显示您的翻唱历史、常用配置、收藏歌曲等信息。
        
        Returns:
            个人统计信息的详细报告
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法查看统计数据。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            stats = []
            stats.append("=" * 60)
            stats.append("📈 个人翻唱统计报告")
            stats.append("=" * 60)
            
            # === 基础统计 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "从未使用")
            
            stats.append(f"\n🔢 基础数据：")
            stats.append(f"   总翻唱次数: {usage_count}")
            stats.append(f"   最后使用: {last_time[:19] if last_time != '从未使用' else last_time}")
            
            # === 当前偏好 ===
            stats.append(f"\n⚙️ 当前偏好设置：")
            stats.append(f"   默认类型: {pref.get('default_api_type', '未设置').upper()}")
            stats.append(f"   默认模型: 第{pref.get('default_model_index', '未设置')}个")
            stats.append(f"   默认音调: {pref.get('default_key_shift', '未设置'):+d}" if isinstance(pref.get('default_key_shift'), int) else "   默认音调: 未设置")
            
            # === 上次使用 ===
            last_model = pref.get("last_used_model", "无")
            last_api = pref.get("last_used_api_type", "无")
            stats.append(f"\n🎯 上次使用：")
            stats.append(f"   模型: {last_model}")
            stats.append(f"   类型: {last_api.upper()}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                stats.append(f"\n❤️ 收藏歌曲 ({len(favorite_songs)}首)：")
                for i, song in enumerate(favorite_songs[-10:], 1):
                    stats.append(f"   {i}. 《{song}》")
            else:
                stats.append(f"\n❤️ 收藏歌曲：暂无")
            
            # === 偏爱歌手 ===
            preferred_artists = pref.get("preferred_artists", [])
            if preferred_artists:
                stats.append(f"\n🎤 偏爱歌手：")
                for artist in preferred_artists:
                    stats.append(f"   • {artist}")
            else:
                stats.append(f"\n🎤 偏爱歌手：暂无")
            
            # === 操作建议 ===
            stats.append(f"\n" + "-" * 60)
            stats.append(f"💡 可用操作：")
            stats.append(f"   • 查看推荐配置：'给我推荐个配置'")
            stats.append(f"   • 修改偏好设置：'把默认模型改成第2个'")
            stats.append(f"   • 清空历史数据：'清空我的翻唱历史'")
            
            stats.append(f"\n" + "=" * 60)
            stats.append(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            stats.append("=" * 60)
            
            return "\n".join(stats)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"查看统计时出错: {e}"

    @filter.llm_tool(name="clear_my_history")
    async def clear_my_history(self, event: AstrMessageEvent) -> str:
        '''清空个人的翻唱历史和偏好数据。

        删除所有与当前用户相关的使用记录、偏好设置、收藏等信息。
        
        Returns:
            清除操作的结果确认
        '''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无需清除数据。"
        
        try:
            user_id = self._get_user_id(event)
            
            if user_id not in self.user_preferences:
                return "没有找到您的任何历史数据。"
            
            del self.user_preferences[user_id]
            self._save_preferences()
            
            return "✅ 已清除您的所有翻唱历史和偏好数据！\n\n包括：\n• 使用统计\n• 偏好设置\n• 收藏歌曲\n• 偏爱歌手记录\n\n下次使用时将从零开始重新学习您的喜好。"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"清除历史时出错: {e}"

    # ==================== 辅助命令 ====================
    
    @filter.command("我的翻唱统计")
    async def view_stats_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看我的翻唱统计'即可！")
            return
        result = await self.view_my_stats(event)
        yield event.plain_result(result)
