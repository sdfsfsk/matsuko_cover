from pathlib import Path
import os
import re
import uuid
import time

import asyncio
import shutil
import json
import secrets
from datetime import datetime
from typing import Optional, Dict, List, Any, Set
import traceback
from contextlib import asynccontextmanager
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
    "RVC/SVC/SoulX-SVCVC翻唱网易云/QQ音乐歌曲（支持LLM智能调用、智能错误反馈、QQ音乐风控自动重试）",
    "2.8.0",
    "https://github.com/sdfsfsk/matsuko_cover",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:3333/")
        self.svc_base_url = config.get("svc_base_url", "http://127.0.0.1:9999/")
        self.svcvc_base_url = config.get("svcvc_base_url", "http://127.0.0.1:6767/")
        
        self.default_api = config.get("default_api", "netease_nodejs")
        self.nodejs_base_url = config.get("nodejs_base_url", "https://163api.qijieya.cn")
        self.enable_qqmusic = config.get("enable_qqmusic", True)
        self.qqmusic_retry_on_ratelimit = config.get("qqmusic_retry_on_ratelimit", True)
        self.qqmusic_retry_max_attempts = config.get("qqmusic_retry_max_attempts", 3)
        self.disable_netease = config.get("disable_netease", False)
        self.timeout = config.get("timeout", 60)
        
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        self.svc_models_keywords = config.get("svc_models_keywords", [])
        self.svcvc_models_keywords = config.get("svcvc_models_keywords", [])
        
        self.enable_rvc = config.get("enable_rvc", True)
        self.enable_svc = config.get("enable_svc", True)
        self.enable_svcvc = config.get("enable_svcvc", False)
        
        self.inference_timeout = config.get("inference_timeout", 300)
        self.llm_tool_timeout = config.get("llm_tool_timeout", 10)
        self.music_api_timeout = max(5, int(config.get("music_api_timeout", 20)))
        
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
            api_key = config.get("third_party_api_key", "")
            self.api = QQMusicAPI(api_key=api_key)
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
        
        # === 方案A+B：上下文注入 + 任务追踪 ===
        self.enable_inject_context = config.get("enable_inject_context", True)
        self.enable_task_tracking = config.get("enable_task_tracking", True)
        self.task_timeout_seconds = config.get("task_timeout_seconds", 9000)
        
        self.default_api_type = config.get("default_api_type", "rvc")
        self.default_model_index = config.get("default_model_index", 1)
        self.default_key_shift = config.get("default_key_shift", 0)
        self.f0_method = config.get("f0_method", "rmvpe")
        self.svc_f0_method = config.get("svc_f0_method", "fcpe")
        try:
            self.index_rate = float(config.get("index_rate", "0.75"))
        except (ValueError, TypeError):
            logger.warning("index_rate 配置格式错误，使用默认值 0.75")
            self.index_rate = 0.75
        self.filter_radius = config.get("filter_radius", 3)
        self.reverb_intensity = config.get("reverb_intensity", 4)
        self.delay_intensity = config.get("delay_intensity", 0)
        self.shift_accompaniment = config.get("shift_accompaniment", True)
        self.vocal_postprocess = bool(config.get("vocal_postprocess", False))

        # === SoulX-Singer SVC Voice Conversion 参数 ===
        self.svcvc_prompt_vocal_sep = bool(config.get("svcvc_prompt_vocal_sep", False))
        self.svcvc_target_vocal_sep = bool(config.get("svcvc_target_vocal_sep", True))
        self.svcvc_auto_shift = bool(config.get("svcvc_auto_shift", True))
        self.svcvc_auto_mix_acc = bool(config.get("svcvc_auto_mix_acc", True))
        try:
            self.svcvc_pitch_shift = max(-36, min(36, int(config.get("svcvc_pitch_shift", 0))))
        except (TypeError, ValueError):
            logger.warning("svcvc_pitch_shift 配置格式错误，使用默认值 0")
            self.svcvc_pitch_shift = 0
        try:
            self.svcvc_n_step = max(1, min(200, int(config.get("svcvc_n_step", 32))))
        except (TypeError, ValueError):
            logger.warning("svcvc_n_step 配置格式错误，使用默认值 32")
            self.svcvc_n_step = 32
        try:
            self.svcvc_cfg = max(0.0, min(10.0, float(config.get("svcvc_cfg", "1.0"))))
        except (TypeError, ValueError):
            logger.warning("svcvc_cfg 配置格式错误，使用默认值 1.0")
            self.svcvc_cfg = 1.0
        try:
            self.svcvc_seed = max(0, min(4294967295, int(config.get("svcvc_seed", 42))))
        except (TypeError, ValueError):
            logger.warning("svcvc_seed 配置格式错误，使用默认值 42")
            self.svcvc_seed = 42
        self.svcvc_random_seed = bool(config.get("svcvc_random_seed", False))
        
        # === MSST 分离参数（仅 RVCSVC-API-MSST 后端生效）===
        # Follow the native recommendation of model_bs_roformer_ep_317_sdr_12.9755.
        # Batch size changes throughput/VRAM, while overlap materially affects
        # separation smoothness.  TTA is optional because it triples inference.
        self.msst_batch_size = config.get("msst_batch_size", 1)
        self.msst_num_overlap = config.get("msst_num_overlap", 4)
        self.msst_normalize = config.get("msst_normalize", False)
        self.msst_use_tta = config.get("msst_use_tta", False)
        self.msst_default_model = config.get(
            "msst_default_model", "bs_roformer_ep_317_sdr_12.9755"
        )
        
        # === UVR5 分离参数（仅 RVCSVC-API-amd 后端生效）===
        self.uvr5_agg = config.get("uvr5_agg", 10)
        self.uvr5_tta = config.get("uvr5_tta", False)
        self.uvr5_postprocess = config.get("uvr5_postprocess", False)
        self.uvr5_window_size = config.get("uvr5_window_size", 512)
        self.uvr5_high_end_process = config.get("uvr5_high_end_process", "mirroring")
        
        self.max_batch_size = config.get("max_batch_size", 5)
        self.preference_storage_path = config.get("preference_storage_path", "data/user_preferences.json")

        self.enable_auto_key_shift = config.get("enable_auto_key_shift", False)
        self.enable_auto_key_shift_debug = config.get("enable_auto_key_shift_debug", False)
        self.gender_detection_timeout = max(
            3, int(config.get("gender_detection_timeout", 15))
        )
        self.male_to_female_shift = config.get("male_to_female_shift", 12)
        self.female_to_male_shift = config.get("female_to_male_shift", -12)
        self.artist_gender_map = self._parse_gender_map(config.get("artist_gender_map", []))
        self.model_gender_map = self._parse_gender_map(config.get("model_gender_map", []))

        # === 方案E：偏好学习系统 ===
        self.user_preferences: Dict[str, Dict] = {}
        self._pref_lock = asyncio.Lock()  # 偏好数据并发锁
        self._load_preferences()
        
        # === 异步任务追踪 ===
        self._pending_tasks: Set[asyncio.Task] = set()
        
        # === 方案B：翻唱任务状态追踪 ===
        self._active_cover_tasks: Dict[str, Dict] = {}
        
        # === 本地音频缓存 ===
        self._recent_audio_cache: Dict[str, tuple[str, float]] = {}
        self._recent_audio_cache_ttl = config.get("local_audio_cache_ttl", 300)
        self.max_local_audio_size_mb = config.get("max_local_audio_size_mb", 30)
        self.enable_local_audio_cover = config.get("enable_local_audio_cover", True)
        self.local_audio_auto_trigger = config.get("local_audio_auto_trigger", False)
        self.local_audio_trigger_keywords = config.get("local_audio_trigger_keywords", ["翻唱", "处理", "修音", "cover"])
        self.local_audio_default_model = config.get("local_audio_default_model", "")
        self.local_audio_default_shift = config.get("local_audio_default_shift", 0)
        self._audio_cache_lock = asyncio.Lock()
        
        # === 清理旧临时文件 ===
        self._cleanup_old_temp_files()
        
        # === 性别识别缓存 ===
        self._gender_cache: Dict[str, str] = {}
        self._gender_cache_max_size = 100
        
        # 动态更新 smart_cover 的 docstring，替换可用引擎占位符
        engines_list = []
        if self.enable_rvc:
            engines_list.append("rvc")
        if self.enable_svc:
            engines_list.append("svc")
        if self.enable_svcvc:
            engines_list.append("svcvc")
        engines_str = ", ".join(engines_list).upper() if engines_list else "无"
        raw_doc = getattr(type(self).smart_cover, "__doc__", None)
        if raw_doc and "{available_engines}" in raw_doc:
            type(self).smart_cover.__doc__ = raw_doc.replace("{available_engines}", engines_str)

    async def _send_cover_result(self, event: AstrMessageEvent, result_path: str, song_name: str = "翻唱", cache_hit: bool = False):
        if not result_path or not os.path.exists(result_path):
            await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
            return
        if cache_hit:
            await event.send(event.plain_result(f"⚡ 命中缓存！这首歌松子之前已经唱过啦，直接发给你nya～"))
        record_sent = False
        file_sent = False
        record_error = None
        try:
            await event.send(event.chain_result([Record(file=result_path)]))
            record_sent = True
            logger.info(f"[MatsukoCover发送Debug] QQ语音发送成功: {result_path}")
        except Exception as e:
            record_error = e
            logger.error(f"QQ语音发送失败，将继续尝试发送文件: {e}")
        # 配置开启时额外发送文件；即使未开启，QQ 语音失败也必须自动兜底。
        if self.enable_send_file or not record_sent:
            try:
                file_name = os.path.basename(result_path)
                name, ext = os.path.splitext(file_name)
                if not ext:
                    ext = ".mp3"
                safe_name = re.sub(r'[\\/:*?"<>|]', '', song_name)
                send_name = f"{safe_name}{ext}"
                await event.send(event.chain_result([File(name=send_name, file=result_path)]))
                file_sent = True
                logger.info(f"[MatsukoCover发送Debug] QQ文件发送成功: {send_name}")
            except Exception as e:
                logger.error(f"以文件形式发送翻唱结果失败: {e}")
        if not record_sent and not file_sent:
            raise RuntimeError(f"QQ语音和文件均发送失败: {record_error or '未知错误'}")
        if not record_sent and file_sent:
            await event.send(event.plain_result("⚠️ QQ语音上传失败，已自动改为发送音频文件。"))

    async def _send_progress_notice(self, event: AstrMessageEvent, message: str) -> None:
        """Best-effort progress notice; a failed notice must not abort inference."""
        try:
            await event.send(event.plain_result(message))
        except Exception as e:
            logger.debug(f"发送翻唱进度提示失败: {e}")

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

    async def _save_preferences(self):
        """保存用户偏好数据（异步安全）"""
        if not self.enable_preference_learning:
            return
            
        async with self._pref_lock:
            try:
                pref_path = Path(self.preference_storage_path)
                pref_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(pref_path, 'w', encoding='utf-8') as f:
                    json.dump(self.user_preferences, f, ensure_ascii=False, indent=2)
                logger.info(f"已保存用户偏好数据: {len(self.user_preferences)} 个用户")
            except Exception as e:
                logger.error(f"保存偏好数据失败: {e}")
    
    def _cleanup_old_temp_files(self):
        """清理超过24小时的旧临时文件"""
        try:
            temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
            if not os.path.isdir(temp_dir):
                return
            now = time.time()
            for fname in os.listdir(temp_dir):
                fpath = os.path.join(temp_dir, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 86400:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"清理旧临时文件失败: {e}")

    def _is_audio_file(self, filename: Optional[str]) -> bool:
        """判断文件名是否为支持的音频格式"""
        if not filename:
            return False
        ext = os.path.splitext(filename.lower())[1]
        return ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".wma", ".aac", ".mp4"}

    def _infer_key_shift_from_filename(self, filename: str, model_display: str = "") -> Optional[int]:
        """根据音频文件名自动推断合适的 key_shift（本地兜底逻辑，不依赖LLM）
        当 LLM 未传入 key_shift 时，优先使用用户配置的映射表和数值进行推断。
        返回 None 表示无法推断（使用默认配置）。
        """
        if not filename:
            return None
        
        basename = os.path.splitext(os.path.basename(filename))[0].lower()
        model_lower = model_display.lower() if model_display else ""
        
        # ===== Step 1: 使用用户配置的歌手性别映射表 =====
        song_gender = None
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in basename:
                song_gender = gender
                logger.info(f"[AutoKeyShift] 文件名匹配歌手映射表: '{artist}' → {gender}")
                break
        
        # ===== Step 2: 如果映射表未命中，用关键词推断 =====
        if not song_gender:
            male_indicators = [
                "周杰伦", "林俊杰", "陈奕迅", "李荣浩", "薛之谦", "毛不易", "张学友",
                "刘德华", "周深", "五月天", "beyond", "陶喆", "方大同", "王力宏",
                "萧敬腾", "杨宗纬", "张敬轩", "胡彦斌", "许嵩", "汪苏泷",
                "听妈妈的话", "晴天", "稻香", "七里香", "青花瓷", "告白气球",
                "夜曲", "简单爱", "不能说的秘密", "蒲公英的约定", "一路向北",
                "江南", "曹操", "背对背拥抱", "可惜没如果", "修炼爱情",
                "十年", "浮夸", "k歌之王", "富士山下",
                "模特", "年少有为", "不将就", "戒烟",
                "演员", "绅士", "认真的雪", "刚刚好",
                "消愁", "像我这样的人", "借", "无问",
                "海阔天空", "光辉岁月", "真的爱你", "喜欢你",
                "普通朋友", "小镇姑娘", "流沙",
                "爱爱爱", "三人游", "特别的人",
                "唯一", "爱你等于爱自己", "龙的传人",
            ]
            female_indicators = [
                "邓紫棋", "王菲", "蔡依林", "张惠妹", "孙燕姿", "田馥甄",
                "梁静茹", "刘若英", "林忆莲", "蔡健雅", "徐佳莹", "周笔畅",
                "张靓颖", "李宇春", "谭维维", "萨顶顶", "阿兰",
                "霉霉", "taylor", "adele", "beyonce", "rihanna",
                "光年之外", "泡沫", "喜欢你", "句号", "倒数",
                "红豆", "传奇", "如愿", "匆匆那年",
                "日不落", "舞娘", "说爱你", "看我72变",
                "听海", "我可以抱你吗", "记得",
                "遇见", "天黑黑", "开始懂了", "我怀念的",
                "小幸运", "你就不要想起我", "寂寞寂寞就好",
                "勇气", "暖暖", "分手快乐", "宁夏",
                "后来", "很爱很爱你", "成全",
            ]
            is_male_song = any(ind.lower() in basename for ind in male_indicators)
            is_female_song = any(ind.lower() in basename for ind in female_indicators)
            if is_male_song:
                song_gender = "male"
            elif is_female_song:
                song_gender = "female"
        
        # 如果仍无法推断歌曲性别，返回 None
        if not song_gender:
            return None
        
        # ===== Step 3: 推断模型性别（优先用户配置的模型性别映射表）=====
        model_gender = None
        for alias, gender in self.model_gender_map.items():
            if alias.lower() in model_lower:
                model_gender = gender
                logger.info(f"[AutoKeyShift] 模型匹配映射表: '{alias}' → {gender}")
                break
        
        # 映射表未命中，用关键词推断
        if not model_gender:
            female_model_keywords = ["甘城", "猫猫", "塔菲", "tafei", "nyaa", "miku", "初音", "镜音", "洛天依", "reol", "ado", "yoasobi", "yorushika", "ayase", "ikura", " LiSA", "美波", "aimer", "花谱", "可不", "星界", "星尘", "心华", "乐正", "墨清弦", "言和", "战音", "绯村", "鹿乃", "hanser", "泠鸢", "冰糖", "七海", "阿梓", "小可", "永雏", "猫雷", "東雪蓮", "明前", "兰音", "扇宝", "虚研", "四禧", "丸子", "EOE", "ASOUL", "乃琳", "贝拉", "珈乐", "嘉然", "向晚", "星瞳", "雫lulu", "真白花音", "早稻叽", "白神遥", "花园", "hiiro", "希月", "奈奈", "米诺", "虞莫", "莞儿", "露早", "柚恩"]
            male_model_keywords = ["kano", "shinichi", "男声", "少年", "大叔", "青年", "正太", "低音", "王爷", "葛平", "波澜", "面筋", "元首", "哲♂学", "比利", "van样", "香蕉君", "刘醒", "梁非凡", "金坷垃", "庞麦郎", "波澜哥", "李云龙", "张涵予", "姜文", "于谦", "郭德纲", "赵本山", "范伟", "沈腾", "黄渤", "徐峥", "王宝强", "刘烨", "张译", "朱亚文", "靳东", "胡歌", "王凯", "肖战", "王一博", "易烊千玺", "王俊凯", "王源", "华晨宇", "毛不易", "周深", "张杰", "薛之谦", "李荣浩", "许嵩", "汪苏泷", "陈奕迅", "林俊杰", "周杰伦"]
            if any(kw in model_lower for kw in female_model_keywords):
                model_gender = "female"
            elif any(kw in model_lower for kw in male_model_keywords):
                model_gender = "male"
        
        # 如果无法推断模型性别，假设为女声模型（大多数AI翻唱模型偏女声）
        if not model_gender:
            model_gender = "female"
        
        # ===== Step 4: 根据性别匹配关系返回配置中的数值 =====
        if song_gender == model_gender:
            return 0  # 同性匹配，保持原调
        elif model_gender == "female" and song_gender == "male":
            return self.male_to_female_shift  # 男→女，使用配置的升调值
        elif model_gender == "male" and song_gender == "female":
            return self.female_to_male_shift  # 女→男，使用配置的降调值
        
        return None

    async def _cleanup_expired_audio_cache(self):
        """清理过期的近期音频缓存"""
        now = time.time()
        expired = []
        async with self._audio_cache_lock:
            for key, (path, ts) in list(self._recent_audio_cache.items()):
                if now - ts > self._recent_audio_cache_ttl:
                    expired.append(key)
                    temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
                    if path.startswith(temp_dir) and os.path.isfile(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            for key in expired:
                self._recent_audio_cache.pop(key, None)

    async def _extract_audio_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息事件中提取音频文件的本地路径"""
        msg_chain = getattr(event.message_obj, "message", [])
        if not msg_chain:
            logger.debug("[LocalAudio] 消息链为空")
            return None
        
        for idx, comp in enumerate(msg_chain):
            comp_type = type(comp).__name__
            logger.debug(f"[LocalAudio] 检查消息组件 {idx}: {comp_type}")
            
            if isinstance(comp, Record):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_url = getattr(comp, "url", None)
                file_name = getattr(comp, "name", "")
                
                logger.info(f"[LocalAudio] 发现语音/音频消息: file={file_path}, url={file_url}")
                
                # 优先使用本地路径
                if file_path and os.path.isfile(file_path):
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    # 尝试从 url 或 filename 提取后缀
                    suffix = ".mp3"
                    if file_name and "." in file_name:
                        suffix = os.path.splitext(file_name)[1]
                    elif file_url and "." in file_url.split("?")[0]:
                        url_ext = os.path.splitext(file_url.split("?")[0])[1]
                        if len(url_ext) <= 5: # 确保是正常的后缀
                            suffix = url_ext
                            
                    downloaded = await self._download_audio(file_url, suffix)
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取
                if file_path:
                    try:
                        record_info = await event.bot.call_action("get_record", file=file_path, out_format="mp3")
                        logger.info(f"[LocalAudio] get_record 返回: {record_info}")
                        if isinstance(record_info, dict):
                            actual_path = record_info.get("file") or record_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                return actual_path
                            actual_url = record_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 获取语音文件失败: {e}")
            
            elif isinstance(comp, File):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_name = getattr(comp, "name", "") or ""
                file_url = getattr(comp, "url", None)
                logger.info(f"[LocalAudio] 发现文件消息: name={file_name}, file={file_path}, url={file_url}")
                
                if not self._is_audio_file(file_name):
                    logger.debug(f"[LocalAudio] 跳过非音频文件: {file_name}")
                    continue
                
                # 优先本地路径
                if file_path and os.path.isfile(file_path):
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    if size_mb > self.max_local_audio_size_mb:
                        logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                        return None
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    downloaded = await self._download_audio(file_url, os.path.splitext(file_name)[1] or ".mp3")
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取文件
                if file_path:
                    try:
                        file_info = await event.bot.call_action("get_file", file_id=file_path)
                        logger.info(f"[LocalAudio] get_file 返回: {file_info}")
                        if isinstance(file_info, dict):
                            actual_path = file_info.get("file") or file_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                size_mb = os.path.getsize(actual_path) / (1024 * 1024)
                                if size_mb > self.max_local_audio_size_mb:
                                    logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                                    return None
                                return actual_path
                            actual_url = file_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, os.path.splitext(file_name)[1] or ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 通过API获取文件失败: {e}")
        
        logger.info(f"[LocalAudio] 未从消息中提取到音频。消息链组件数: {len(msg_chain)}")
        return None

    async def _download_audio(self, url: str, suffix: str = ".mp3") -> Optional[str]:
        """下载音频文件到临时目录"""
        try:
            import aiohttp
            temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
            os.makedirs(temp_dir, exist_ok=True)
            dest = os.path.join(temp_dir, f"dl_{uuid.uuid4().hex[:8]}{suffix}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        max_bytes = int(float(self.max_local_audio_size_mb) * 1024 * 1024)
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if content_length > max_bytes:
                            logger.warning(
                                f"下载的音频过大: {content_length / 1024 / 1024:.1f}MB，已拒绝"
                            )
                            return None
                        written = 0
                        with open(dest, "wb") as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                written += len(chunk)
                                if written > max_bytes:
                                    raise ValueError(
                                        f"下载的音频超过 {self.max_local_audio_size_mb}MB 限制"
                                    )
                                f.write(chunk)
                        size_mb = os.path.getsize(dest) / (1024 * 1024)
                        if size_mb > self.max_local_audio_size_mb:
                            logger.warning(f"下载的音频过大: {size_mb:.1f}MB，已丢弃")
                            os.remove(dest)
                            return None
                        return dest
                    else:
                        logger.warning(f"下载音频失败: HTTP {resp.status}")
        except Exception as e:
            if 'dest' in locals() and os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            logger.error(f"下载音频异常: {e}")
        return None

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建被追踪的异步任务，防止任务丢失和异常静默"""
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "background")
        task = asyncio.create_task(coro, name=f"matsuko_cover:{coro_name}")
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        logger.info(
            f"[MatsukoCover任务Debug] 已创建后台任务: {task.get_name()} | "
            f"pending={len(self._pending_tasks)}"
        )

        def _log_exception(t: asyncio.Task):
            if t.cancelled():
                logger.warning(f"[MatsukoCover任务Debug] 后台任务已取消: {t.get_name()}")
            elif t.exception() is not None:
                logger.error(f"[MatsukoCover] 异步任务异常: {t.exception()}", exc_info=t.exception())
            else:
                logger.info(f"[MatsukoCover任务Debug] 后台任务已完成: {t.get_name()}")

        task.add_done_callback(_log_exception)
        return task
    
    @asynccontextmanager
    async def _get_gradio_client(self, base_url: str):
        """安全获取Gradio Client，确保正确关闭"""
        client = None
        try:
            # Client 初始化会同步读取 /config；放到线程池，避免阻塞 AstrBot 事件循环。
            client = await asyncio.to_thread(
                Client, base_url, verbose=False, analytics_enabled=False
            )
            yield client
        finally:
            if client is not None:
                try:
                    await asyncio.to_thread(client.close)
                except Exception as e:
                    logger.debug(f"关闭Gradio Client时出错: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识"""
        return str(event.get_sender_id())

    def _get_user_pref(self, user_id: str) -> Dict:
        """获取用户偏好，如果不存在则创建默认值"""
        if user_id not in self.user_preferences:
            self.user_preferences[user_id] = {
                "default_api_type": self.default_api_type,
                "default_model_index": max(1, self.default_model_index),
                "default_key_shift": self.default_key_shift,
                "favorite_songs": [],
                "usage_count": 0,
                "last_used_time": None,
                "preferred_artists": {},
                "artist_model_map": {}
            }
        pref = self.user_preferences[user_id]
        if isinstance(pref.get("preferred_artists"), list):
            old_list = pref["preferred_artists"]
            pref["preferred_artists"] = {a: {"count": 1, "last_time": None, "model": None} for a in old_list}
        if "artist_model_map" not in pref:
            pref["artist_model_map"] = {}
        if pref.get("default_model_index", 0) < 1:
            pref["default_model_index"] = max(1, self.default_model_index)
        return pref

    async def _async_predict(self, client, *args, timeout=300, event=None, detect_cache_hit=False, **kwargs):
        logger.debug(f"[UVR5 Debug] 传参: uvr5_agg={kwargs.get('uvr5_agg')}, uvr5_tta={kwargs.get('uvr5_tta')}, uvr5_postprocess={kwargs.get('uvr5_postprocess')}, uvr5_window_size={kwargs.get('uvr5_window_size')}, uvr5_high_end_process={kwargs.get('uvr5_high_end_process')}")
        logger.info(
            "[MSST分离参数Debug] 插件提交参数: "
            f"model={kwargs.get('msst_model', 'backend-default')} | "
            f"batch={kwargs.get('msst_batch_size', 'backend-default')} | "
            f"overlap={kwargs.get('msst_num_overlap', 'backend-default')} | "
            f"normalize={kwargs.get('msst_normalize', 'backend-default')} | "
            f"tta={kwargs.get('msst_use_tta', 'backend-default')}"
        )
        loop = asyncio.get_running_loop()
        job = await asyncio.to_thread(client.submit, *args, **kwargs)
        self._bind_active_gradio_job(event, job)
        timeout_seconds = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout_seconds

        def cancel_job_best_effort():
            """Ask Gradio to stop queued/running work without masking the timeout."""
            try:
                cancel = getattr(job, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception as cancel_error:
                logger.debug(f"推理超时后取消 Gradio 任务失败: {cancel_error}")

        def raise_inference_timeout(cause=None):
            cancel_job_best_effort()
            message = (
                f"推理任务在 {timeout_seconds:g} 秒内未完成，"
                "已尝试取消后端任务，请检查推理服务状态或调大任务超时时间。"
            )
            if cause is None:
                raise TimeoutError(message)
            raise TimeoutError(message) from cause
        
        cache_hit_detected = False
        
        if self.enable_progress_bar and event:
            import re as _re
            last_msg_time = 0
            last_desc = ""
            while not job.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Avoid timing out a job that completed between done() and
                    # the monotonic deadline check.
                    if job.done():
                        break
                    raise_inference_timeout()
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
                        
                        # 检测缓存命中
                        if desc and ('cache hit' in desc.lower() or '缓存命中' in desc):
                            cache_hit_detected = True
                        
                        current_time = time.time()
                        if desc and desc != last_desc:
                            if current_time - last_msg_time >= self.progress_update_interval or last_msg_time == 0:
                                has_sub_pct = bool(_re.search(r'\d+%', desc))
                                if has_sub_pct:
                                    msg = f"{self._get_stage_emoji(desc)} {desc}"
                                else:
                                    overall_pct = f" [{int(pct * 100)}%]" if pct is not None else ""
                                    msg = f"{self._get_stage_emoji(desc)} {desc}{overall_pct}"
                                self._create_tracked_task(event.send(event.plain_result(msg)))
                                last_msg_time = current_time
                                last_desc = desc
                except Exception as e:
                    logger.debug(f"获取进度信息时出错: {e}")
                await asyncio.sleep(min(1.0, max(0.0, remaining)))
                
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not job.done():
            raise_inference_timeout()

        # A completed Gradio job returns immediately.  Otherwise both
        # job.result() and asyncio.wait_for() share the same monotonic budget.
        result_timeout = max(0.0, remaining) if not job.done() else 0
        fn = partial(job.result, timeout=result_timeout)
        result_future = loop.run_in_executor(None, fn)
        try:
            if job.done():
                result = await result_future
            else:
                result = await asyncio.wait_for(result_future, timeout=remaining)
        except TimeoutError as exc:
            raise_inference_timeout(exc)
        
        # 解析中间层返回的缓存标记（元组格式: (file_path, "true"/"false")）
        cache_hit_from_api = False
        if detect_cache_hit and isinstance(result, (list, tuple)) and len(result) >= 2:
            cache_flag = str(result[1]).lower()
            cache_hit_from_api = cache_flag == "true"
            result = result[0]
        
        # 如果之前未检测到缓存命中，再尝试从最终状态确认（备用方案）
        if detect_cache_hit and not cache_hit_detected and not cache_hit_from_api:
            try:
                final_status = job.status()
                if hasattr(final_status, 'progress_data') and final_status.progress_data:
                    for pd in final_status.progress_data:
                        desc = getattr(pd, 'desc', None)
                        if desc is None and isinstance(pd, dict):
                            desc = pd.get('desc')
                        if desc and ('cache hit' in desc.lower() or '缓存命中' in desc):
                            cache_hit_detected = True
                            break
            except Exception as e:
                logger.debug(f"最终状态检测缓存时出错: {e}")
        
        if detect_cache_hit:
            self._clear_active_gradio_job(event, job)
            return result, cache_hit_from_api or cache_hit_detected
        self._clear_active_gradio_job(event, job)
        return result

    async def _predict_cover(
        self,
        event: AstrMessageEvent,
        api_type: str,
        song_name_src: str,
        model_name: str,
        key_shift: Optional[int],
    ) -> tuple[Any, bool, Optional[int]]:
        """Submit one conversion using the parameter contract of the selected engine."""
        api_type = str(api_type).lower()
        base_url = self._get_engine_base_url(api_type)
        effective_seed: Optional[int] = None

        async with self._get_gradio_client(base_url) as client:
            if api_type == "svcvc":
                effective_seed = (
                    secrets.randbelow(4294967296)
                    if self.svcvc_random_seed
                    else self.svcvc_seed
                )
                pitch_shift = self._effective_key_shift(api_type, key_shift)
                logger.info(
                    "[SoulX-SVCVC参数Debug] "
                    f"profile={model_name} | prompt_sep={self.svcvc_prompt_vocal_sep} | "
                    f"target_sep={self.svcvc_target_vocal_sep} | auto_shift={self.svcvc_auto_shift} | "
                    f"auto_mix={self.svcvc_auto_mix_acc} | pitch_shift={pitch_shift} | "
                    f"n_step={self.svcvc_n_step} | cfg={self.svcvc_cfg} | "
                    f"seed={effective_seed} | random={self.svcvc_random_seed}"
                )
                result, cache_hit = await self._async_predict(
                    client,
                    song_name_src=song_name_src,
                    model_dropdown=model_name,
                    prompt_vocal_sep=self.svcvc_prompt_vocal_sep,
                    target_vocal_sep=self.svcvc_target_vocal_sep,
                    auto_shift=self.svcvc_auto_shift,
                    auto_mix_acc=self.svcvc_auto_mix_acc,
                    pitch_shift=pitch_shift,
                    n_step=self.svcvc_n_step,
                    cfg=self.svcvc_cfg,
                    seed=effective_seed,
                    random_seed=self.svcvc_random_seed,
                    api_name="/convert",
                    timeout=self.inference_timeout,
                    event=event,
                    detect_cache_hit=True,
                )
                return result, cache_hit, effective_seed

            legacy_key_shift = self._effective_key_shift(api_type, key_shift)
            msst_kwargs = await self._get_optional_msst_kwargs(client, base_url)
            legacy_kwargs = {
                "song_name_src": song_name_src,
                "key_shift": legacy_key_shift,
                "vocal_vol": 0,
                "inst_vol": 0,
                "model_dropdown": model_name,
                "reverb_intensity": self.reverb_intensity,
                "delay_intensity": self.delay_intensity,
                "uvr5_agg": self.uvr5_agg,
                "uvr5_tta": self.uvr5_tta,
                "uvr5_postprocess": self.uvr5_postprocess,
                "uvr5_window_size": self.uvr5_window_size,
                "uvr5_high_end_process": self.uvr5_high_end_process,
                "msst_batch_size": self.msst_batch_size,
                "msst_num_overlap": self.msst_num_overlap,
                "msst_normalize": self.msst_normalize,
                "vocal_postprocess": self.vocal_postprocess,
                "shift_accompaniment": self.shift_accompaniment,
                **msst_kwargs,
            }
            if api_type == "svc":
                legacy_kwargs["svc_f0_method"] = self.svc_f0_method
            else:
                legacy_kwargs.update({
                    "f0_method": self.f0_method,
                    "index_rate": self.index_rate,
                    "filter_radius": self.filter_radius,
                })
            result, cache_hit = await self._async_predict(
                client,
                **legacy_kwargs,
                api_name="/convert",
                timeout=self.inference_timeout,
                event=event,
                detect_cache_hit=True,
            )
            return result, cache_hit, None

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

    # ==================== 方案A+B：上下文注入 + 任务追踪辅助方法 ====================

    def _get_task_key(self, event: AstrMessageEvent) -> str:
        """生成任务追踪的唯一键（按会话维度）"""
        return str(event.unified_msg_origin)

    def _cleanup_expired_tasks(self):
        """清理超时的翻唱任务"""
        if not self.enable_task_tracking:
            return
        now = time.time()
        expired_keys = []
        for key, task_info in self._active_cover_tasks.items():
            start_time = task_info.get("start_time", 0)
            if now - start_time > self.task_timeout_seconds:
                expired_keys.append(key)
        for key in expired_keys:
            task_info = self._active_cover_tasks.pop(key, None) or {}
            job = task_info.get("gradio_job")
            task = task_info.get("asyncio_task")
            try:
                if job is not None:
                    job.cancel()
            except Exception as exc:
                logger.debug(f"清理超时 Gradio 任务失败: {exc}")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
            logger.warning(f"[MatsukoCover] 任务超时已清理: {key}")

    def _register_active_task(self, event: AstrMessageEvent, song_name: str, api_type: str, model_display: str = "") -> bool:
        """注册正在进行的翻唱任务"""
        if not self.enable_task_tracking:
            return True
        self._cleanup_expired_tasks()
        key = self._get_task_key(event)
        if key in self._active_cover_tasks:
            return False
        self._active_cover_tasks[key] = {
            "song_name": song_name,
            "api_type": api_type,
            "model_display": model_display,
            "status": "处理中",
            "start_time": time.time(),
            "asyncio_task": None,
            "gradio_job": None,
        }
        logger.info(f"[MatsukoCover] 注册翻唱任务: {key} -> 《{song_name}》({api_type})")
        return True

    def _bind_active_asyncio_task(self, event: AstrMessageEvent, task: Optional[asyncio.Task] = None) -> None:
        """把真实 asyncio 任务绑定到会话状态，供取消命令使用。"""
        if not self.enable_task_tracking:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None:
            info["asyncio_task"] = task or asyncio.current_task()

    def _bind_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None:
            info["gradio_job"] = job

    def _clear_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None and info.get("gradio_job") is job:
            info["gradio_job"] = None

    def _finish_active_task(self, event: AstrMessageEvent, status: str = "已完成"):
        """标记翻唱任务结束"""
        if not self.enable_task_tracking:
            return
        key = self._get_task_key(event)
        if key in self._active_cover_tasks:
            self._active_cover_tasks[key]["status"] = status
            self._active_cover_tasks.pop(key, None)
            logger.info(f"[MatsukoCover] 翻唱任务结束: {key} -> {status}")

    def _get_active_task_status(self, event: AstrMessageEvent) -> Optional[Dict]:
        """获取当前会话的活跃任务状态"""
        if not self.enable_task_tracking:
            return None
        self._cleanup_expired_tasks()
        key = self._get_task_key(event)
        return self._active_cover_tasks.get(key)

    async def _inject_message_to_context(self, event: AstrMessageEvent, content: str, role: str = "assistant"):
        """将消息注入到 LLM 的对话上下文中（方案A）"""
        if not self.enable_inject_context:
            return
        try:
            conv_mgr = self.context.conversation_manager
            unified_msg_origin = event.unified_msg_origin
            cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
            if not cid:
                logger.debug("[MatsukoCover] 未找到当前对话ID，无法注入上下文")
                return
            conv = await conv_mgr.get_conversation(unified_msg_origin, cid)
            if not conv:
                logger.debug("[MatsukoCover] 未找到对话对象，无法注入上下文")
                return
            import json
            history = json.loads(conv.history) if conv.history else []
            history.append({"role": role, "content": content})
            await conv_mgr.update_conversation(unified_msg_origin, cid, history=history)
            logger.debug(f"[MatsukoCover] 已注入{role}消息到上下文: {content[:50]}...")
        except Exception as e:
            logger.error(f"[MatsukoCover] 注入上下文失败: {e}")

    async def _notify_llm_with_context(self, event: AstrMessageEvent, song_name: str, status_type: str, result_detail: str = ""):
        """通知 LLM 翻唱结果，并将通知内容注入上下文（方案A+B整合）"""
        if not self.enable_llm_success_notify:
            return None
        
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.debug("[MatsukoCover] 未找到 provider，无法通知 LLM")
                return None
            
            if status_type == "failed":
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）处理失败了！失败原因：{result_detail}。请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个坏消息，并建议用户重试或换一首歌（不要包含系统通知字样）。"
            elif status_type == "cache_hit":
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）命中了缓存，音频已经直接发送给用户了！请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个好消息，可以俏皮地说'这首歌我之前就唱过啦'（不要包含系统通知字样）。"
            else:  # success
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）已经由后台处理完成，并且音频已经发送给用户了！请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个好消息（不要包含系统通知字样）。"
            
            llm_resp = await provider.text_chat(prompt=prompt)
            reply_text = llm_resp.completion_text if llm_resp else ""
            
            if reply_text:
                await event.send(event.plain_result(reply_text))
                # 方案A：将通知内容注入 LLM 上下文
                await self._inject_message_to_context(event, reply_text, role="assistant")
            
            return reply_text
        except Exception as e:
            logger.error(f"[MatsukoCover] 通知 LLM 失败: {e}")
            return None

    def _get_engine_models(self, api_type: str) -> list:
        api_type = str(api_type or "").lower()
        if api_type == "svc":
            return self.svc_models_keywords
        if api_type == "svcvc":
            return self.svcvc_models_keywords
        return self.rvc_models_keywords

    def _set_engine_models(self, api_type: str, models: list) -> None:
        api_type = str(api_type or "").lower()
        if api_type == "svc":
            self.svc_models_keywords = models
            self.config["svc_models_keywords"] = models
        elif api_type == "svcvc":
            self.svcvc_models_keywords = models
            self.config["svcvc_models_keywords"] = models
        else:
            self.rvc_models_keywords = models
            self.config["rvc_models_keywords"] = models

    def _get_engine_base_url(self, api_type: str) -> str:
        api_type = str(api_type or "").lower()
        if api_type == "svc":
            return self.svc_base_url
        if api_type == "svcvc":
            return self.svcvc_base_url
        return self.rvc_base_url

    def _is_engine_enabled(self, api_type: str) -> bool:
        api_type = str(api_type or "").lower()
        return {
            "rvc": self.enable_rvc,
            "svc": self.enable_svc,
            "svcvc": self.enable_svcvc,
        }.get(api_type, False)

    def _get_available_engines(self) -> list[str]:
        return [name for name in ("rvc", "svc", "svcvc") if self._is_engine_enabled(name)]

    @staticmethod
    def _engine_display_name(api_type: str) -> str:
        return "SoulX-SVCVC" if str(api_type).lower() == "svcvc" else str(api_type).upper()

    @staticmethod
    def _key_shift_range(api_type: str) -> tuple[int, int]:
        return (-36, 36) if str(api_type).lower() == "svcvc" else (-12, 12)

    def _effective_key_shift(self, api_type: str, key_shift: Optional[int]) -> int:
        if str(api_type).lower() == "svcvc" and key_shift is None:
            return self.svcvc_pitch_shift
        return int(key_shift or 0)

    def get_models_display_list(self, api_type="rvc"):
        models_keywords = self._get_engine_models(api_type)
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = alias or os.path.splitext(model_name)[0]
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    @staticmethod
    def _format_cache_bytes(value: Any) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "未知"
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TiB"

    async def _backend_cache_request(self, api_type: str, clear: bool = False) -> Any:
        base_url = self._get_engine_base_url(api_type)
        async with self._get_gradio_client(base_url) as client:
            if clear:
                return await self._async_predict(
                    client, scope="all", api_name="/clear_cache", timeout=60
                )
            return await self._async_predict(
                client, api_name="/cache_info", timeout=30
            )

    @staticmethod
    def _parse_cache_target(message: str) -> list[str]:
        normalized = str(message or "").strip().lower()
        for command in ("查看翻唱缓存", "清理翻唱缓存"):
            normalized = re.sub(rf"^/?{command}\s*", "", normalized).strip()
        aliases = {
            "rvc": ["rvc"],
            "svc": ["svc"],
            "svcvc": ["svcvc"],
            "soulx": ["svcvc"],
            "all": ["rvc", "svc", "svcvc"],
            "全部": ["rvc", "svc", "svcvc"],
            "": ["rvc", "svc", "svcvc"],
        }
        return aliases.get(normalized, [])

    def get_models_detailed_list(self, api_type="rvc"):
        """获取包含完整信息的模型列表（用于LLM匹配）"""
        models_keywords = self._get_engine_models(api_type)
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

    def _parse_gender_map(self, entries: list) -> Dict[str, str]:
        result = {}
        for entry in entries:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            parts = entry.split(":", 1)
            name = parts[0].strip()
            gender = parts[1].strip().lower()
            if name and gender in ("male", "female"):
                result[name] = gender
        return result

    def _add_to_gender_cache(self, cache_key: str, value: str):
        """安全地写入性别缓存，防止无限增长"""
        if len(self._gender_cache) >= self._gender_cache_max_size:
            keys_to_remove = list(self._gender_cache.keys())[:self._gender_cache_max_size // 2]
            for k in keys_to_remove:
                del self._gender_cache[k]
        self._gender_cache[cache_key] = value

    def _detect_artist_gender(self, artist_name: str) -> Optional[str]:
        if not artist_name:
            return None
        artist_lower = artist_name.strip().lower()
        for mapped_name, gender in self.artist_gender_map.items():
            if mapped_name.strip().lower() == artist_lower:
                return gender
        return None

    async def _detect_artist_gender_llm(self, artist_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not artist_name:
            return None
        cache_key = f"artist:{artist_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌手「{artist_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_artist_gender(artist_name)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：歌手「{artist_name}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider（event={'有' if event else '无'}），跳过歌手性别判断")
                return None
            prompt = (
                f"请判断歌手或组合「{artist_name}」的主唱性别。"
                f"如果是组合，请判断主唱（唱主要部分的人）的性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果不确定，请优先根据该名字在日语/华语/欧美流行乐坛的常见用法进行推断，"
                f"不要仅凭字面含义判断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌手「{artist_name}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"「{artist_name}」的主唱是男性还是女性？"
                        f"这是一位歌手或音乐组合，请结合流行音乐常识判断。"
                        f"只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌手「{artist_name}」性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌手「{artist_name}」性别")
        except Exception as e:
            logger.error(f"LLM 判断歌手性别失败: {e}")
        return None

    def _detect_model_gender(self, model_display: str) -> Optional[str]:
        if not model_display:
            return None
        model_lower = model_display.strip().lower()
        for mapped_name, gender in self.model_gender_map.items():
            if mapped_name.strip().lower() == model_lower:
                logger.info(f"自动升降调：模型「{model_display}」在映射表中找到，性别={gender}")
                return gender
        return None

    async def _detect_model_gender_llm(self, model_display: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not model_display:
            return None
        cache_key = f"model:{model_display.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：模型「{model_display}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_model_gender(model_display)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：模型「{model_display}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过模型性别判断")
                return None
            prompt = (
                f"AI语音模型「{model_display}」模拟的角色是男性还是女性？"
                f"只回答'男'或'女'。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断模型「{model_display}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断模型「{model_display}」性别")
        except Exception as e:
            logger.error(f"LLM 判断模型性别失败: {e}")
        return None

    async def _detect_gender_pair(
        self,
        source_name: str,
        model_display: str,
        event: AstrMessageEvent = None,
        source_is_song: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        """并发识别来源与模型性别；LLM 迟迟不返回时快速回退。"""
        source_label = "歌曲" if source_is_song else "歌手"

        async def _with_timeout(coro, label: str) -> Optional[str]:
            try:
                return await asyncio.wait_for(
                    coro, timeout=self.gender_detection_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[自动升降调Debug] {label}识别超过 "
                    f"{self.gender_detection_timeout} 秒，已跳过并继续翻唱"
                )
                return None
            except Exception as exc:
                logger.warning(f"[自动升降调Debug] {label}识别失败，已回退: {exc}")
                return None

        source_coro = (
            self._detect_song_gender_llm(source_name, event)
            if source_is_song
            else self._detect_artist_gender_llm(source_name, event)
        )
        logger.info(
            f"[自动升降调Debug] 开始并发识别{source_label}与模型性别 | "
            f"timeout={self.gender_detection_timeout}s | "
            f"source={source_name} | model={model_display}"
        )
        source_gender, model_gender = await asyncio.gather(
            _with_timeout(source_coro, source_label),
            _with_timeout(
                self._detect_model_gender_llm(model_display, event), "模型"
            ),
        )
        logger.info(
            f"[自动升降调Debug] 性别识别结束 | {source_label}={source_gender} | "
            f"模型={model_gender}"
        )
        return source_gender, model_gender

    async def _detect_song_gender_llm(self, song_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        """根据歌曲名判断原唱歌手性别（用于本地音频翻唱的自动升降调）"""
        if not song_name:
            return None
        cache_key = f"song:{song_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌曲「{song_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        
        # Step 1: 先尝试从 artist_gender_map 中匹配歌曲名中的歌手名
        song_lower = song_name.lower()
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in song_lower:
                logger.info(f"自动升降调：歌曲「{song_name}」匹配歌手映射表 '{artist}' → {gender}")
                self._add_to_gender_cache(cache_key, gender)
                return gender
        
        # Step 2: 本地关键词快速推断（常见女声/男声歌曲标记）
        female_song_markers = ["believers", "so'fly", "miku", "初音", "洛天依", "宅舞", " anime", "动漫", "acg", "女声", "女版", "翻唱版(女)", "cover by 女", "version by 女"]
        male_song_markers = ["男声", "男版", "cover by 男", "version by 男"]
        if any(m in song_lower for m in female_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配女声关键词 → female")
            self._add_to_gender_cache(cache_key, "female")
            return "female"
        if any(m in song_lower for m in male_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配男声关键词 → male")
            self._add_to_gender_cache(cache_key, "male")
            return "male"
        
        # Step 3: LLM 判断（兜底）
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过歌曲性别判断")
                return None
            prompt = (
                f"请判断歌曲「{song_name}」的原唱歌手性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果是日语/动漫/JPOP歌曲，通常女声演唱者较多；如果是华语流行摇滚，男性演唱者较多。"
                f"如果不确定，请优先根据该歌曲在JPOP/动漫/华语流行乐坛的常见演唱者进行推断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌曲「{song_name}」原唱性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"歌曲「{song_name}」的原唱是男性还是女性？"
                        f"这是一首JPOP/动漫/流行歌曲，请结合音乐常识判断。只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌曲「{song_name}」原唱性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌曲「{song_name}」原唱性别")
        except Exception as e:
            logger.error(f"LLM 判断歌曲原唱性别失败: {e}")
        return None

    async def _calc_auto_key_shift(self, artist_name: str, model_display: str, user_key_shift: int, artist_gender: Optional[str] = None, model_gender: Optional[str] = None, event: AstrMessageEvent = None, detection_attempted: bool = False) -> int:
        if not self.enable_auto_key_shift:
            return user_key_shift
        if user_key_shift != 0:
            return user_key_shift

        if not artist_gender and not detection_attempted:
            artist_gender = await self._detect_artist_gender_llm(artist_name, event)
        if not model_gender and not detection_attempted:
            model_gender = await self._detect_model_gender_llm(model_display, event)

        gender_label = {"male": "男", "female": "女", None: "未知"}
        final_shift = user_key_shift
        debug_reason = ""

        if not artist_gender or not model_gender:
            logger.info(f"自动升降调：无法确定性别（歌手={artist_gender}, 模型={model_gender}），跳过")
            debug_reason = "无法确定性别，跳过自动升降调"
        elif artist_gender == model_gender:
            logger.info(f"自动升降调：歌手和模型性别相同（{artist_gender}），无需调整")
            debug_reason = f"性别相同({gender_label.get(artist_gender, '?')})，无需调整"
        elif model_gender == "female" and artist_gender == "male":
            final_shift = self.male_to_female_shift
            logger.info(f"自动升降调：男→女，升调 {final_shift}")
            debug_reason = f"男歌手 → 女模型，自动升调 {final_shift:+d}"
        elif model_gender == "male" and artist_gender == "female":
            final_shift = self.female_to_male_shift
            logger.info(f"自动升降调：女→男，降调 {final_shift}")
            debug_reason = f"女歌手 → 男模型，自动降调 {final_shift:+d}"
        else:
            debug_reason = "未匹配到自动升降调规则"

        if self.enable_auto_key_shift_debug and event:
            try:
                debug_msg = (
                    f"🎵 [自动升降调Debug]\n"
                    f"歌手：{artist_name} → {gender_label.get(artist_gender, '未知')}\n"
                    f"模型：{model_display} → {gender_label.get(model_gender, '未知')}\n"
                    f"结果：{debug_reason}"
                )
                await event.send(event.plain_result(debug_msg))
            except Exception:
                pass

        return final_shift

    async def _update_models_from_api(self, api_type="rvc"):
        base_url = self._get_engine_base_url(api_type)
        profile_display_names: Dict[str, str] = {}
        async with self._get_gradio_client(base_url) as client:
            try:
                model_list_from_api = await self._async_predict(client, api_name="/show_model")
            except Exception as e:
                logger.warning(f"第一次获取模型列表失败，重试中: {e}")
                model_list_from_api = await self._async_predict(client, api_name="/show_model")
            if api_type == "svcvc":
                try:
                    profile_details = await self._async_predict(
                        client, api_name="/list_voice_profiles"
                    )
                    if isinstance(profile_details, list):
                        for profile in profile_details:
                            if not isinstance(profile, dict):
                                continue
                            profile_id = str(
                                profile.get("profile_id") or profile.get("id") or ""
                            ).strip()
                            display_name = str(
                                profile.get("display_name") or profile.get("name") or ""
                            ).strip()
                            if profile_id and display_name and display_name != profile_id:
                                profile_display_names[profile_id] = display_name
                    logger.info(
                        "[SVCVC音色列表Debug] 插件获取音色详情: "
                        f"{profile_display_names or '(无自定义显示名)'}"
                    )
                except Exception as exc:
                    logger.info(
                        f"[SVCVC音色列表Debug] 后端未提供音色详情，继续使用 ID: {exc}"
                    )

        if not isinstance(model_list_from_api, list):
            raise ValueError(f"获取模型列表失败: {model_list_from_api}")

        current_config_list = self._get_engine_models(api_type)
        old_aliases = {}
        for item_str in current_config_list:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [
            f"{m}{MODEL_ALIAS_SEPARATOR}"
            f"{old_aliases.get(m, '') or profile_display_names.get(m, '')}"
            for m in model_list_from_api
        ]
        
        self._set_engine_models(api_type, new_models_list)
        
        self.config.save_config()
        logger.info(
            f"{self._engine_display_name(api_type)} 模型/音色列表已更新并成功保存，"
            f"共 {len(new_models_list)} 个"
        )

    @staticmethod
    def _client_supports_api(client, api_name: str) -> Optional[bool]:
        """Inspect the fetched Gradio config without issuing an extra request."""
        endpoints = getattr(client, "endpoints", None)
        if not endpoints:
            return None
        values = endpoints.values() if isinstance(endpoints, dict) else endpoints
        names = []
        for endpoint in values:
            name = getattr(endpoint, "api_name", None)
            if name:
                names.append("/" + str(name).lstrip("/"))
        if not names:
            return None
        return "/" + api_name.lstrip("/") in names

    async def _get_msst_models_from_client(self, client) -> List[Dict[str, str]]:
        result = await self._async_predict(
            client, api_name="/show_msst_models", timeout=30
        )
        if not isinstance(result, list):
            raise ValueError(f"后端返回格式错误: {result}")

        models = []
        for item in result:
            if isinstance(item, dict) and item.get("id"):
                models.append({
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                })
            elif isinstance(item, str):
                models.append({"id": item, "name": item})
        if not models:
            raise ValueError("后端没有发现已安装的 MSST 模型")
        return models

    async def _get_optional_msst_kwargs(self, client, base_url: str) -> Dict[str, Any]:
        """Only send the new parameter to an MSST-capable backend."""
        supports = self._client_supports_api(client, "/show_msst_models")
        if supports is True:
            return {
                "msst_model": self.msst_default_model,
                "msst_use_tta": self.msst_use_tta,
            }
        if supports is False:
            logger.info(f"后端 {base_url} 未提供 MSST 模型接口，按 RVCSVC-API-amd 兼容模式调用")
            return {}

        try:
            await self._get_msst_models_from_client(client)
            return {
                "msst_model": self.msst_default_model,
                "msst_use_tta": self.msst_use_tta,
            }
        except Exception:
            logger.info(f"后端 {base_url} 无法确认 MSST 能力，省略 msst_model 参数")
            return {}

    async def _get_msst_models_from_api(self) -> List[Dict[str, str]]:
        """读取 MSST 后端公开的分离模型白名单。"""
        errors = []
        urls = []
        for base_url in (self.rvc_base_url, self.svc_base_url):
            if base_url and base_url not in urls:
                urls.append(base_url)

        for base_url in urls:
            try:
                async with self._get_gradio_client(base_url) as client:
                    supports = self._client_supports_api(client, "/show_msst_models")
                    if supports is False:
                        raise RuntimeError("当前是 RVCSVC-API-amd 或其他非 MSST 后端")
                    models = await self._get_msst_models_from_client(client)
                    model_ids = ", ".join(item["id"] for item in models)
                    logger.info(
                        f"[MSST模型列表Debug] 插件已从 {base_url} 获取 "
                        f"{len(models)} 个模型: {model_ids}"
                    )
                    return models
            except Exception as exc:
                errors.append(f"{base_url}: {exc}")

        raise RuntimeError(
            "当前配置的后端不支持 MSST 模型列表，或 MSST 服务尚未启动。"
            + (f" 详情：{'；'.join(errors)}" if errors else "")
        )

    async def _sync_msst_model_selection(self, model_id: str) -> Optional[str]:
        """Notify one active MSST backend so it can emit an immediate switch debug line."""
        urls = []
        for base_url in (self.rvc_base_url, self.svc_base_url):
            if base_url and base_url not in urls:
                urls.append(base_url)

        for base_url in urls:
            try:
                async with self._get_gradio_client(base_url) as client:
                    supports = self._client_supports_api(client, "/select_msst_model")
                    if supports is False:
                        continue
                    result = await self._async_predict(
                        client,
                        model_name=model_id,
                        api_name="/select_msst_model",
                        timeout=30,
                    )
                    if isinstance(result, dict) and result.get("success"):
                        logger.info(
                            f"[MSST模型切换Debug] 中间层 {base_url} 已确认切换为 {model_id}"
                        )
                        return base_url
            except Exception as exc:
                logger.debug(f"同步 MSST 模型到后端 {base_url} 失败，已忽略: {exc}")
        return None

    # ==================== 命令处理（支持LLM强制模式） ====================

    @filter.command("查看翻唱缓存")
    async def show_cover_cache(self, event: AstrMessageEvent):
        """查看各中间层当前缓存占用。"""
        targets = self._parse_cache_target(event.message_str)
        if not targets:
            yield event.plain_result("用法：/查看翻唱缓存 [all|rvc|svc|svcvc]")
            return
        yield event.plain_result("🔎 正在读取中间层缓存状态，请稍候...")
        lines = []
        for api_type in targets:
            label = self._engine_display_name(api_type)
            try:
                info = await self._backend_cache_request(api_type, clear=False)
                if not isinstance(info, dict):
                    raise RuntimeError(f"后端返回了无法识别的数据: {info!r}")
                lines.append(
                    f"{label}: {int(info.get('total_files', 0))} 个文件 / "
                    f"{self._format_cache_bytes(info.get('total_bytes', 0))}"
                )
                for area, stats in (info.get("areas") or {}).items():
                    lines.append(
                        f"  - {area}: {int(stats.get('files', 0))} 个 / "
                        f"{self._format_cache_bytes(stats.get('bytes', 0))}"
                    )
            except Exception as exc:
                lines.append(f"{label}: 无法读取（{exc}）")
        yield event.plain_result("📦 翻唱缓存状态：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清理翻唱缓存")
    async def clear_cover_cache(self, event: AstrMessageEvent):
        """清理选定中间层的结果、分离和下载缓存。"""
        targets = self._parse_cache_target(event.message_str)
        if not targets:
            yield event.plain_result("用法：/清理翻唱缓存 [all|rvc|svc|svcvc]")
            return
        yield event.plain_result("🧹 正在安全清理中间层缓存，请稍候...")
        lines = []
        for api_type in targets:
            label = self._engine_display_name(api_type)
            try:
                result = await self._backend_cache_request(api_type, clear=True)
                if not isinstance(result, dict):
                    raise RuntimeError(f"后端返回了无法识别的数据: {result!r}")
                lines.append(
                    f"{label}: 删除 {int(result.get('deleted_files', 0))} 个文件，"
                    f"释放 {self._format_cache_bytes(result.get('freed_bytes', 0))}"
                )
            except Exception as exc:
                lines.append(f"{label}: 清理失败（{exc}）")
        yield event.plain_result("✅ 缓存清理完成：\n" + "\n".join(lines))

    @filter.command("列出msst模型")
    async def list_msst_models(self, event: AstrMessageEvent):
        """列出当前 RVCSVC-API-MSST 后端安装的分离模型。"""
        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            yield event.plain_result(f"ℹ️ 当前后端没有可列出的 MSST 模型：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        yield event.plain_result(
            "可用的 MSST 人声分离模型：\n"
            + "\n".join(lines)
            + "\n\n管理员可使用：/切换msst模型 <序号或模型名>"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换msst模型")
    async def switch_msst_model(self, event: AstrMessageEvent):
        """查看或切换 RVCSVC-API-MSST 的默认人声分离模型。"""
        raw = event.message_str.strip()
        requested = re.sub(r"^/?切换msst模型\s*", "", raw, flags=re.IGNORECASE).strip()

        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            logger.error(f"获取 MSST 模型列表失败: {exc}")
            yield event.plain_result(f"❌ 无法读取 MSST 模型列表：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        if not requested:
            yield event.plain_result(
                "当前 MSST 默认模型："
                f"{self.msst_default_model}\n\n可用模型：\n"
                + "\n".join(lines)
                + "\n\n用法：/切换msst模型 <序号或模型名>"
            )
            return

        selected = None
        if requested.isdigit():
            model_index = int(requested) - 1
            if 0 <= model_index < len(models):
                selected = models[model_index]
        else:
            lowered = requested.lower()
            exact = [
                item for item in models
                if lowered in (item["id"].lower(), item["name"].lower())
            ]
            partial = [
                item for item in models
                if lowered in item["id"].lower() or lowered in item["name"].lower()
            ]
            candidates = exact or partial
            if len(candidates) == 1:
                selected = candidates[0]

        if selected is None:
            yield event.plain_result(
                f"❌ 没有唯一匹配的 MSST 模型：{requested}\n\n" + "\n".join(lines)
            )
            return

        self.msst_default_model = selected["id"]
        self.config["msst_default_model"] = self.msst_default_model
        self.config.save_config()
        synced_backend = await self._sync_msst_model_selection(self.msst_default_model)
        sync_text = f"\n中间层确认：{synced_backend}" if synced_backend else ""
        yield event.plain_result(
            f"✅ MSST 默认模型已切换为：{selected['name']}\n"
            f"模型 ID：{selected['id']}\n"
            "之后的新翻唱任务会使用该模型；已有缓存会按模型分别保存。"
            + sync_text
        )
    
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

    @filter.command("刷新svcvc音色")
    async def refresh_svcvc_profiles(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 正在读取 SoulX-SVCVC 参考音色列表，请稍候...")
        try:
            await self._update_models_from_api(api_type="svcvc")
            display_str, _ = self.get_models_display_list(api_type="svcvc")
            display_str = display_str or "未发现任何参考音色。"
            chain = [Plain(f"当前 SoulX-SVCVC 可用参考音色：\n{display_str}")]
            node = Node(uin=1109587454, name="松子", content=chain)
            await event.send(event.chain_result([node]))
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"读取 SoulX-SVCVC 参考音色失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置svcvc后端链接")
    async def set_svcvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置svcvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(
                f"当前 SoulX-SVCVC 中间层：{self.svcvc_base_url}\n"
                "用法：/设置svcvc后端链接 <URL>"
            )
            return
        new_url = args[0]
        if not new_url.endswith("/"):
            new_url += "/"
        self.svcvc_base_url = new_url
        self.config["svcvc_base_url"] = new_url
        self.config.save_config()
        yield event.plain_result(f"SoulX-SVCVC 中间层链接已设置为：{new_url}")

    @filter.command("svcvc")
    async def svcvc(self, event: AstrMessageEvent):
        if not self.enable_svcvc:
            yield event.plain_result(
                "❌ SoulX-SVCVC 已在配置中禁用，请开启 enable_svcvc。"
            )
            return
        if self.llm_force_mode:
            yield event.plain_result(
                "当前已开启 LLM 强制模式，/svcvc 命令已禁用。"
                "请直接对松子说“用 SoulX 参考音色翻唱《歌名》”。"
            )
            return
        if self.disable_netease:
            yield event.plain_result(
                "网易云点歌功能已禁用，请使用 /qqsvcvc <歌名>。"
            )
            return
        async for result in self._handle_cover(event, api_type="svcvc"):
            yield result

    @filter.command("qqrvc")
    async def qqrvc(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/qqrvc 命令已被禁用。\n请直接对我说'用QQ音乐翻唱《歌名》'，我会自动帮您处理！")
            return
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
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/qqsvc 命令已被禁用。\n请直接对我说'用QQ音乐和SVC翻唱《歌名》'，我会自动帮您处理！")
            return
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

    @filter.command("qqsvcvc")
    async def qqsvcvc(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result(
                "当前已开启 LLM 强制模式，/qqsvcvc 命令已禁用。"
                "请直接对松子说“用 QQ 音乐和 SoulX 参考音色翻唱《歌名》”。"
            )
            return
        if not self.enable_svcvc:
            yield event.plain_result(
                "❌ SoulX-SVCVC 已在配置中禁用，请开启 enable_svcvc。"
            )
            return
        if not self.enable_qqmusic:
            yield event.plain_result("❌ QQ 音乐功能未启用，请开启 enable_qqmusic。")
            return
        if not QQ_MUSIC_AVAILABLE:
            yield event.plain_result(
                "❌ qqmusic-api-python 未安装，请先安装该依赖。"
            )
            return
        async for result in self._handle_qq_cover(event, api_type="svcvc"):
            yield result

    @filter.command("qq点歌")
    async def qq_search(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/qq点歌 命令已被禁用。\n请直接对我说'搜索QQ音乐《歌名》'，我会自动帮您处理！")
            return
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
        from .api import QQMusicAPI
        api_key = self.config.get("third_party_api_key", "")
        api = QQMusicAPI(api_key=api_key)
        try:
            songs = await api.fetch_data(keyword=keyword, limit=5)
            if not songs:
                yield event.plain_result(f"在QQ音乐未找到与 '{keyword}' 相关的歌曲。")
                return
            lines = [f"🎵 在QQ音乐找到 {len(songs)} 首相关歌曲：", ""]
            for i, song in enumerate(songs, 1):
                duration_sec = song.get("duration", 0) // 1000
                minutes, seconds = divmod(duration_sec, 60)
                lines.append(f"{i}. {song['name']} - {song['artists']} ({minutes}:{seconds:02d})")
            lines.append("")
            lines.append("💡 使用 /qqrvc、/qqsvc 或 /qqsvcvc <歌名> 进行翻唱")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"QQ音乐搜索失败: {e}")
            yield event.plain_result(f"搜索失败: {e}")
        finally:
            await api.close()

    @filter.command("本地翻唱")
    async def local_audio_cover_cmd(self, event: AstrMessageEvent):
        if not self.enable_local_audio_cover:
            yield event.plain_result("❌ 本地音频翻唱功能未启用。")
            return
        
        audio_path = await self._extract_audio_from_event(event)
        
        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]
        
        if not audio_path:
            yield event.plain_result("❌ 未检测到音频文件或语音消息。\n\n用法：\n1. 在同一条消息中发送音频+文字 /本地翻唱\n2. 先发送语音/音频，然后在5分钟内发送 /本地翻唱\n\n可选参数：[模型名/序号] [rvc/svc/svcvc] [升降调]\n示例：/本地翻唱 官方示例音色 svcvc 0")
            return
        
        args = event.message_str.replace("本地翻唱", "").strip().split()
        model_name = None
        api_type = self.default_api_type
        key_shift = self.default_key_shift
        key_shift_specified = False
        
        for arg in args:
            if arg.lstrip('-').isdigit() and -36 <= int(arg) <= 36:
                key_shift = int(arg)
                key_shift_specified = True
            elif arg.lower() in ("rvc", "svc", "svcvc"):
                api_type = arg.lower()
            elif len(arg) >= 1:
                model_name = arg

        if api_type == "svcvc" and not key_shift_specified:
            key_shift = None
        
        if not self._is_engine_enabled(api_type):
            available = ", ".join(self._get_available_engines()).upper() or "无"
            yield event.plain_result(
                f"❌ {self._engine_display_name(api_type)} 功能已禁用。当前可用引擎：{available}"
            )
            return
        
        await self._execute_local_cover(event, audio_path, model_name, api_type=api_type, key_shift=key_shift)

    async def _handle_cover(self, event: AstrMessageEvent, api_type="rvc"):
        cmd = api_type
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调]")
            return

        key_shift, song_name = (None if api_type == "svcvc" else 0), " ".join(args)
        if args and args[-1].lstrip('-').isdigit():
            try:
                val = int(args[-1])
                min_shift, max_shift = self._key_shift_range(api_type)
                if min_shift <= val <= max_shift:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError: pass
        
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        songs = await asyncio.wait_for(
            self.api.fetch_data(keyword=song_name, limit=10),
            timeout=self.music_api_timeout,
        )
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
            refresh_cmd = "/刷新svcvc音色" if api_type == "svcvc" else f"/刷新{api_type}模型"
            yield event.plain_result(
                f"当前没有可用的 {self._engine_display_name(api_type)} 模型/音色，"
                f"请先使用 {refresh_cmd}。"
            )
            return
        
        chain=[Plain(f"已选歌曲: {selected_song['name']}\n使用: {self._engine_display_name(api_type)}\n\n可用模型/参考音色：\n{display_str}")]
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

        yield event.plain_result(f"正在使用 {self._engine_display_name(api_type)} 模型/音色【{selected_model}】为您生成《{selected_song['name']}》，请耐心等待...")
        await self._send_song(event=event, song=selected_song, model_name=selected_model, key_shift=key_shift, api_type=api_type)

    async def _handle_qq_cover(self, event: AstrMessageEvent, api_type="rvc"):
        cmd = {"rvc": "qqrvc", "svc": "qqsvc", "svcvc": "qqsvcvc"}.get(api_type, f"qq{api_type}")
        args = event.message_str.replace(cmd, "").strip().split()
        
        if not args:
            yield event.plain_result(f"用法: /{cmd} <歌名> [升降调]")
            return

        key_shift, song_name = (None if api_type == "svcvc" else 0), " ".join(args)
        if args and args[-1].lstrip('-').isdigit():
            try:
                val = int(args[-1])
                min_shift, max_shift = self._key_shift_range(api_type)
                if min_shift <= val <= max_shift:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError: pass
        
        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        from .api import QQMusicAPI
        api_key = self.config.get("third_party_api_key", "")
        qq_api = QQMusicAPI(api_key=api_key)
        try:
            songs = await asyncio.wait_for(
                qq_api.fetch_data(keyword=song_name, limit=10),
                timeout=self.music_api_timeout,
            )
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
                refresh_cmd = "/刷新svcvc音色" if api_type == "svcvc" else f"/刷新{api_type}模型"
                yield event.plain_result(
                    f"当前没有可用的 {self._engine_display_name(api_type)} 模型/音色，"
                    f"请先使用 {refresh_cmd}。"
                )
                return
            
            chain=[Plain(f"[QQ音乐] 已选歌曲: {selected_song['name']}\n使用: {self._engine_display_name(api_type)}\n\n可用模型/参考音色：\n{display_str}")]
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

            yield event.plain_result(f"🎵 正在使用 {self._engine_display_name(api_type)} 模型/音色【{selected_model}】为您生成《{selected_song['name']}》（QQ音乐），请耐心等待...")
            await self._send_song(event=event, song=selected_song, model_name=selected_model, key_shift=key_shift, api_type=api_type)
        finally:
            await qq_api.close()

    async def _send_selection(self, event: AstrMessageEvent, songs: list):
        formatted_songs = [f"{i + 1}. {s['name']} - {s['artists']}" for i, s in enumerate(songs[:10])]
        chain=[Plain("为您找到以下歌曲：\n" + "\n".join(formatted_songs))]
        node = Node(uin=1109587454, name="松子", content=chain)
        await event.send(event.chain_result([node]))

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: Optional[int], api_type="rvc"):
        result_path = None
        temp_audio_file = None
        effective_seed = None
        song_name = song.get("name", "翻唱")
        if not self._register_active_task(event, song_name, api_type, model_name):
            await event.send(event.plain_result("⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)
        try:
            # 判断是否为QQ音乐歌曲（通过songmid字段判断）
            is_qq_music = "songmid" in song and song.get("songmid")
            
            if is_qq_music:
                # QQ音乐：下载音频到本地临时文件（避免中文文件名问题）
                import aiohttp
                from .api import QQMusicAPI
                
                api_key = self.config.get("third_party_api_key", "")
                qq_api = QQMusicAPI(api_key=api_key)
                try:
                    songmid = song.get("songmid", "")
                    song_name = song.get("name", "")
                    await self._send_progress_notice(
                        event,
                        f"🔗 正在获取 QQ 音乐《{song_name}》的播放链接...",
                    )
                    
                    # === QQ音乐获取播放链接重试机制 ===
                    audio_url = None
                    if self.qqmusic_retry_on_ratelimit:
                        last_fetch_error = None
                        retry_sent = False
                        for attempt in range(1, self.qqmusic_retry_max_attempts + 1):
                            try:
                                audio_url = await qq_api.fetch_song_url(songmid, song_name=song_name)
                                if audio_url:
                                    break
                                logger.warning(f"QQ音乐获取播放链接返回空，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                                if attempt < self.qqmusic_retry_max_attempts:
                                    # 第一次重试时通知群聊
                                    if not retry_sent:
                                        try:
                                            await event.send(event.plain_result(f"⏳ QQ音乐获取播放链接遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                            retry_sent = True
                                        except:
                                            pass
                                    await asyncio.sleep(5)
                            except Exception as e:
                                last_fetch_error = str(e)
                                logger.warning(f"QQ音乐获取播放链接异常: {e}，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                                if attempt < self.qqmusic_retry_max_attempts:
                                    # 第一次重试时通知群聊
                                    if not retry_sent:
                                        try:
                                            await event.send(event.plain_result(f"⏳ QQ音乐获取播放链接遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                            retry_sent = True
                                        except:
                                            pass
                                    await asyncio.sleep(5)
                        
                        if not audio_url:
                            error_detail = f" (最后一次错误: {last_fetch_error})" if last_fetch_error else ""
                            error_msg = f"❌ QQ音乐源获取失败: 无法获取《{song_name}》的播放链接。已重试{self.qqmusic_retry_max_attempts}次仍失败。可能原因：1) 该歌曲是VIP专享 2) QQ音乐API触发风控 3) 网络问题。建议换一首歌或稍后再试。{error_detail}"
                            logger.error(error_msg)
                            await event.send(event.plain_result(error_msg))
                            return
                    else:
                        audio_url = await qq_api.fetch_song_url(songmid, song_name=song_name)
                    
                    if not audio_url:
                        error_msg = f"❌ QQ音乐源获取失败: 无法获取《{song_name}》的播放链接。可能原因：1) 该歌曲是VIP专享 2) QQ音乐API限制 3) 网络问题。建议换一首歌或稍后再试。"
                        logger.error(error_msg)
                        await event.send(event.plain_result(error_msg))
                        return
                    
                    await self._send_progress_notice(
                        event,
                        f"📥 正在下载 QQ 音乐《{song_name}》，下载完成后会自动提交 SoulX...",
                    )
                    temp_audio_file = await self._download_audio(audio_url, ".mp3")
                    if not temp_audio_file:
                        error_msg = "❌ QQ音乐下载失败或文件超过大小限制，请换一首歌或稍后再试。"
                        logger.error(error_msg)
                        await event.send(event.plain_result(error_msg))
                        return
                    await self._send_progress_notice(
                        event,
                        f"✅ QQ 音乐下载完成（{os.path.getsize(temp_audio_file) / 1024 / 1024:.1f} MiB），正在提交中间层...",
                    )
                    
                    # 使用本地文件路径
                    song_input = temp_audio_file
                finally:
                    await qq_api.close()
            else:
                # 网易云等平台：使用ID或名称
                song_input = song.get("name", str(song.get("id", "unknown")))
                if isinstance(song.get("id"), int) or (isinstance(song.get("id"), str) and song["id"].isdigit()):
                    song_input = str(song["id"])
            
            result_path, cache_hit, effective_seed = await self._predict_cover(
                event=event,
                api_type=api_type,
                song_name_src=song_input,
                model_name=model_name,
                key_shift=key_shift,
            )
            
            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=song_name, cache_hit=cache_hit)
                if api_type == "svcvc" and effective_seed is not None:
                    await event.send(event.plain_result(
                        f"🎲 SoulX-SVCVC 本次实际种子：{effective_seed}"
                        f"（{'随机' if self.svcvc_random_seed else '固定'}）"
                    ))
            else:
                await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                await event.send(event.plain_result(f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。如果需要，请在配置文件中调高 'inference_timeout' 的值。"))
            else:
                await event.send(event.plain_result(f"生成时发生严重错误: {e}"))
        finally:
            self._finish_active_task(event)
            if self.enable_send_file:
                await asyncio.sleep(3)
            if result_path and os.path.isfile(result_path):
                try: os.remove(result_path)
                except OSError as e: logger.debug(f"删除临时文件失败（文件可能仍被占用）: {e}")
            if temp_audio_file and os.path.isfile(temp_audio_file):
                try: os.remove(temp_audio_file)
                except OSError as e: logger.error(f"删除临时音频文件失败: {e}")

    async def _execute_local_cover(self, event: AstrMessageEvent, audio_path: str,
                                    model_name: Optional[str] = None,
                                    model_index: int = 1,
                                    api_type: str = "rvc",
                                    key_shift: Optional[int] = 0):
        """本地音频翻唱核心执行逻辑"""
        if not os.path.exists(audio_path):
            await event.send(event.plain_result("❌ 音频文件不存在或已过期，请重新发送音频。"))
            return
        
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            await event.send(event.plain_result(f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"))
            return
        
        if not self._is_engine_enabled(api_type):
            await event.send(event.plain_result(
                f"❌ {self._engine_display_name(api_type)} 功能已禁用。"
            ))
            return
        
        models_keywords = self._get_engine_models(api_type)
        if not models_keywords:
            refresh_cmd = "/刷新svcvc音色" if api_type == "svcvc" else f"/刷新{api_type}模型"
            await event.send(event.plain_result(
                f"❌ 当前没有可用的 {self._engine_display_name(api_type)} 模型/音色，"
                f"请先使用 {refresh_cmd}。"
            ))
            return
        
        if model_name:
            matched = self._find_model_index_by_name(model_name, api_type)
            if matched:
                model_index = matched
            else:
                await event.send(event.plain_result(
                    f"❌ 未找到匹配的 {self._engine_display_name(api_type)} 模型/音色：'{model_name}'"
                ))
                return
        
        if not (1 <= model_index <= len(models_keywords)):
            await event.send(event.plain_result(f"❌ 模型序号无效，当前共有 {len(models_keywords)} 个模型。"))
            return
        
        min_shift, max_shift = self._key_shift_range(api_type)
        if key_shift is not None and not (min_shift <= key_shift <= max_shift):
            await event.send(event.plain_result(
                f"❌ 音调调整值无效，请在 {min_shift} 到 {max_shift} 之间选择。"
            ))
            return
        
        _, keys = self.get_models_display_list(api_type=api_type)
        selected_model = keys[model_index - 1]
        models_info = self.get_models_detailed_list(api_type)
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break
        
        song_display_name = os.path.splitext(os.path.basename(audio_path))[0]
        if song_display_name.startswith("qq_") or song_display_name.startswith("dl_"):
            song_display_name = "本地音频"
        
        if not self._register_active_task(event, song_display_name, api_type, model_display):
            await event.send(event.plain_result("⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)
        
        result_path = None
        effective_seed = None
        try:
            engine_display = self._engine_display_name(api_type)
            await event.send(event.plain_result(f"🎵 正在用【{model_display}】({engine_display}) 处理您的音频，请稍候..."))
            
            result_path, cache_hit, effective_seed = await self._predict_cover(
                event=event,
                api_type=api_type,
                song_name_src=audio_path,
                model_name=selected_model,
                key_shift=key_shift,
            )
            
            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=song_display_name, cache_hit=cache_hit)
                
                result_msg = f"✅ 已成功使用 {engine_display} 音色【{selected_model}】处理您的音频！"
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！{result_msg}"
                if api_type == "svcvc" and effective_seed is not None:
                    result_msg += (
                        f"\n🎲 实际种子：{effective_seed}"
                        f"（{'随机' if self.svcvc_random_seed else '固定'}）"
                    )
                if self.enable_config_report:
                    actual_shift = self._effective_key_shift(api_type, key_shift)
                    result_msg += f"\n\n📊 本次配置：类型={engine_display}, 模型={model_display}, 调音={actual_shift:+d}"
                await event.send(event.plain_result(result_msg))
                await self._notify_llm_with_context(event, song_display_name, "cache_hit" if cache_hit else "success")
            else:
                await event.send(event.plain_result("❌ 处理失败，后端未返回有效文件路径。"))
                await self._notify_llm_with_context(event, song_display_name, "failed", result_detail="后端未返回文件路径")
                
        except Exception as e:
            logger.error(traceback.format_exc())
            error_msg = f"处理时发生错误: {e}"
            if "Timeout" in str(e):
                error_msg = f"⏳ 处理超时！后端在 {self.inference_timeout} 秒内没有完成任务。"
            await event.send(event.plain_result(error_msg))
            await self._notify_llm_with_context(event, song_display_name, "failed", result_detail=str(e))
        finally:
            self._finish_active_task(event)
            temp_dir = os.path.join(os.path.dirname(__file__), "temp_audio")
            if audio_path.startswith(temp_dir) and os.path.isfile(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            if result_path and os.path.isfile(result_path):
                try:
                    if self.enable_send_file:
                        await asyncio.sleep(3)
                    os.remove(result_path)
                except OSError:
                    pass

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
        api_to_use = self.api
        platform_name = "网易云音乐"
        _qq_api_inst = None
        
        try:
            limit = min(max(limit, 1), 10)
            
            if platform.lower() in ['qq', 'qqmusic', 'qq音乐', 'qq音乐']:
                if not self.enable_qqmusic:
                    return "❌ QQ音乐功能未启用！请在插件配置中开启 'enable_qqmusic' 开关。"
                if not QQ_MUSIC_AVAILABLE:
                    return "❌ qqmusic-api-python 库未安装！请联系管理员安装。"
                
                from .api import QQMusicAPI
                api_key = self.config.get("third_party_api_key", "")
                _qq_api_inst = QQMusicAPI(api_key=api_key)
                api_to_use = _qq_api_inst
                platform_name = "QQ音乐"
            elif platform.lower() in ['netease', '网易云', 'netease_cloud', 'default']:
                if self.disable_netease:
                    return "❌ 网易云音乐功能已被禁用！请使用 platform='qq' 或 platform='qqmusic' 搜索QQ音乐。"
            
            songs = await asyncio.wait_for(
                api_to_use.fetch_data(keyword=keyword, limit=limit),
                timeout=self.music_api_timeout,
            )
            
            if not songs:
                if platform_name == "QQ音乐":
                    return f"❌ 在QQ音乐未找到与 '{keyword}' 相关的歌曲。可能原因：1) QQ音乐API连接失败 2) 该歌曲不存在于QQ音乐 3) 网络问题。建议尝试用网易云音乐搜索（platform='netease'）或换一首歌名。"
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
        finally:
            if _qq_api_inst is not None:
                await _qq_api_inst.close()

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

        if not self._register_active_task(event, song_name, "rvc", model_display):
            return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
        logger.info(
            f"[MatsukoCover任务Debug] 接受 rvc_cover 请求 | song={song_name} | "
            f"model={model_display} | source={actual_music_source}"
        )
        task = self._create_tracked_task(self._smart_cover_async(
            event, search_query, "rvc", model_index, key_shift, model_display, actual_music_source
        ))
        self._bind_active_asyncio_task(event, task)
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

        if not self._register_active_task(event, song_name, "svc", model_display):
            return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
        logger.info(
            f"[MatsukoCover任务Debug] 接受 svc_cover 请求 | song={song_name} | "
            f"model={model_display} | source={actual_music_source}"
        )
        task = self._create_tracked_task(self._smart_cover_async(
            event, search_query, "svc", model_index, key_shift, model_display, actual_music_source
        ))
        self._bind_active_asyncio_task(event, task)
        return f"🎵 正在从【{source_display}】用SVC模型【{model_display}】翻唱《{song_name}》... 请稍等，音频生成后会自动发送！"

    @filter.llm_tool(name="svcvc_cover")
    async def svcvc_cover(
        self,
        event: AstrMessageEvent,
        song_name: str,
        artist_name: Optional[str] = None,
        model_index: int = 1,
        model_name: Optional[str] = None,
        key_shift: Optional[int] = None,
        music_source: Optional[str] = None,
    ) -> str:
        '''使用 SoulX-Singer SVC Voice Conversion 参考音色翻唱歌曲。

        Args:
            song_name(string): 要翻唱的歌曲名称
            artist_name(string, optional): 指定歌手或版本
            model_index(number): 参考音色序号，从1开始
            model_name(string, optional): 参考音色名称或别名，优先于序号
            key_shift(number, optional): 手动变调，范围-36到36；不传时使用插件配置，显式传0会覆盖配置并交给SoulX自动匹配音域
            music_source(string, optional): 'netease' 或 'qqmusic'

        Returns:
            后台任务启动结果；完成后会自动发送音频和实际种子
        '''
        if not self.enable_svcvc:
            return "❌ SoulX-SVCVC 已禁用，请在插件配置中开启 enable_svcvc。"

        if model_name:
            matched = self._find_model_index_by_name(model_name, "svcvc")
            if matched:
                model_index = matched
            else:
                return (
                    f"❌ 未找到匹配的 SoulX-SVCVC 参考音色：'{model_name}'\n\n"
                    "💡 可使用 get_available_models('svcvc') 查看音色列表"
                )

        models_info = self.get_models_detailed_list("svcvc")
        model_display = next(
            (m["display"] for m in models_info if m["index"] == model_index), ""
        )
        if not model_display:
            return "❌ 参考音色序号无效，请先读取 SoulX-SVCVC 音色列表。"

        actual_music_source = music_source or self.default_api
        if self.disable_netease and actual_music_source in ("netease", "netease_nodejs"):
            actual_music_source = "qqmusic"
        source_display = "QQ音乐" if actual_music_source == "qqmusic" else "网易云"
        search_query = f"{song_name} {artist_name}" if artist_name else song_name

        if not self._register_active_task(event, song_name, "svcvc", model_display):
            return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
        logger.info(
            f"[MatsukoCover任务Debug] 接受 svcvc_cover 请求 | song={song_name} | "
            f"profile={model_display} | source={actual_music_source}"
        )
        task = self._create_tracked_task(self._smart_cover_async(
            event,
            search_query,
            "svcvc",
            model_index,
            key_shift,
            model_display,
            actual_music_source,
        ))
        self._bind_active_asyncio_task(event, task)
        seed_mode = "随机种子" if self.svcvc_random_seed else f"固定种子 {self.svcvc_seed}"
        return (
            f"🎵 正在从【{source_display}】用 SoulX-SVCVC 参考音色"
            f"【{model_display}】翻唱《{song_name}》（{seed_mode}）... "
            "完成后会自动发送音频！"
        )

    async def _do_cover(self, event: AstrMessageEvent, song_name: str, api_type: str, model_index: int, key_shift: Optional[int], music_source: str = None) -> str:
        """统一的翻唱执行逻辑"""
        flow_id = uuid.uuid4().hex[:8]
        result_path = None
        temp_audio_file = None
        temporary_search_api = None
        try:
            logger.info(
                f"[CoverFlow:{flow_id}] stage=start | song={song_name} | "
                f"api={api_type} | model_index={model_index} | source={music_source}"
            )
            if not self._is_engine_enabled(api_type):
                available = ", ".join(self._get_available_engines()).upper() or "无"
                return (
                    f"❌ {self._engine_display_name(api_type)} 功能已在配置中禁用。"
                    f"当前可用引擎：{available}"
                )
            if api_type != "svcvc" and key_shift is None:
                key_shift = self.default_key_shift
            
            models_keywords = self._get_engine_models(api_type)
            
            if not models_keywords:
                refresh_cmd = "/刷新svcvc音色" if api_type == "svcvc" else f"/刷新{api_type}模型"
                return (
                    f"当前没有可用的 {self._engine_display_name(api_type)} 模型/音色，"
                    f"请先使用 {refresh_cmd} 更新列表。"
                )
            
            if not (1 <= model_index <= len(models_keywords)):
                return f"模型序号无效，当前共有 {len(models_keywords)} 个模型可选，请输入 1 到 {len(models_keywords)} 之间的数字。"
            
            min_shift, max_shift = self._key_shift_range(api_type)
            if key_shift is not None and not (min_shift <= key_shift <= max_shift):
                return f"音调调整值无效，请在 {min_shift} 到 {max_shift} 之间选择。"
            
            # === 根据音乐源选择 API ===
            actual_music_source = music_source or self.default_api
            if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
                actual_music_source = "qqmusic"
            if actual_music_source == "qqmusic":
                if not self.enable_qqmusic:
                    return "❌ QQ音乐功能未启用"
                from .api import QQMusicAPI
                api_key = self.config.get("third_party_api_key", "")
                search_api = QQMusicAPI(api_key=api_key)
                temporary_search_api = search_api
            elif actual_music_source in ["netease", "netease_nodejs"]:
                if self.disable_netease:
                    return "❌ 网易云音乐功能已禁用"
                search_api = self.api
            else:
                search_api = self.api
            
            # === QQ音乐风控重试机制 ===
            songs = []
            if actual_music_source == "qqmusic" and self.qqmusic_retry_on_ratelimit:
                # 带重试的QQ音乐搜索
                last_error = None
                retry_sent = False
                for attempt in range(1, self.qqmusic_retry_max_attempts + 1):
                    try:
                        logger.info(
                            f"[CoverFlow:{flow_id}] stage=music_search | "
                            f"source={actual_music_source} | attempt={attempt}"
                        )
                        songs = await asyncio.wait_for(
                            search_api.fetch_data(keyword=song_name, limit=10),
                            timeout=self.music_api_timeout,
                        )
                        if songs:
                            break  # 搜索成功，跳出重试循环
                        # 返回空列表，可能是风控或真的没结果
                        logger.warning(f"QQ音乐搜索返回空列表，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                        if attempt < self.qqmusic_retry_max_attempts:
                            # 第一次重试时通知群聊
                            if not retry_sent:
                                try:
                                    await event.send(event.plain_result(f"⏳ QQ音乐搜索遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                    retry_sent = True
                                except:
                                    pass
                            await asyncio.sleep(5)  # 等待5秒后重试
                    except Exception as e:
                        last_error = str(e)
                        logger.warning(f"QQ音乐搜索异常: {e}，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                        if attempt < self.qqmusic_retry_max_attempts:
                            # 第一次重试时通知群聊
                            if not retry_sent:
                                try:
                                    await event.send(event.plain_result(f"⏳ QQ音乐搜索遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                    retry_sent = True
                                except:
                                    pass
                            await asyncio.sleep(5)  # 等待5秒后重试
                
                if not songs:
                    error_detail = f" (最后一次错误: {last_error})" if last_error else ""
                    return f"❌ QQ音乐搜索失败: 无法找到《{song_name}》。已重试{self.qqmusic_retry_max_attempts}次仍失败。可能原因：1) QQ音乐API触发风控，需进行登录或安全验证 2) 该歌曲不存在于QQ音乐 3) 网络问题。建议尝试用网易云音乐搜索（music_source='netease'）或稍后再试。{error_detail}"
            else:
                # 普通搜索（无重试）
                logger.info(
                    f"[CoverFlow:{flow_id}] stage=music_search | "
                    f"source={actual_music_source} | timeout={self.music_api_timeout}s"
                )
                songs = await asyncio.wait_for(
                    search_api.fetch_data(keyword=song_name, limit=10),
                    timeout=self.music_api_timeout,
                )

            logger.info(
                f"[CoverFlow:{flow_id}] stage=music_search_done | count={len(songs)}"
            )
            
            # 关闭临时创建的 QQMusicAPI 搜索实例
            if actual_music_source == "qqmusic" and hasattr(search_api, 'close'):
                await search_api.close()
                temporary_search_api = None
            
            if not songs:
                if actual_music_source == "qqmusic":
                    return f"❌ QQ音乐搜索失败: 无法找到《{song_name}》。可能原因：1) QQ音乐API触发风控，需进行登录或安全验证 2) 该歌曲不存在于QQ音乐 3) 网络问题。建议尝试用网易云音乐搜索（music_source='netease'）或稍后再试。"
                return f"❌ 未找到歌曲 '{song_name}'。"
            
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
            
            # === 自动升降调 ===
            models_info = self.get_models_detailed_list(api_type)
            model_display = ""
            for m in models_info:
                if m["index"] == model_index:
                    model_display = m["display"]
                    break
            
            original_key_shift = key_shift
            artist_from_song = selected_song.get("artists", "")
            artist_gender = None
            model_gender = None
            detection_attempted = False
            if api_type != "svcvc" and self.enable_auto_key_shift and key_shift == 0:
                detection_attempted = True
                logger.info(f"[CoverFlow:{flow_id}] stage=gender_detection")
                artist_gender, model_gender = await self._detect_gender_pair(
                    artist_from_song, model_display, event
                )
            if api_type != "svcvc":
                key_shift = await self._calc_auto_key_shift(
                    artist_from_song,
                    model_display,
                    key_shift,
                    artist_gender,
                    model_gender,
                    event,
                    detection_attempted=detection_attempted,
                )
            if key_shift != original_key_shift and self.enable_auto_key_shift:
                logger.info(f"自动升降调：{artist_from_song} → {model_display}，音调从 {original_key_shift:+d} 调整为 {key_shift:+d}")
            
            # === QQ音乐：先下载音频到本地文件 ===
            song_input_for_api = str(selected_song["id"])
            if actual_music_source == "qqmusic":
                from .api import QQMusicAPI
                api_key = self.config.get("third_party_api_key", "")
                qq_api = QQMusicAPI(api_key=api_key)
                try:
                    songmid = selected_song.get("songmid") or selected_song["id"]
                    song_display_name = selected_song.get("name", song_name)
                    
                    logger.info(f"QQ音乐: 获取播放链接 {songmid} - {song_display_name}")
                    await self._send_progress_notice(
                        event,
                        f"🔗 正在获取 QQ 音乐《{song_display_name}》的播放链接...",
                    )
                    
                    # === QQ音乐获取播放链接重试机制 ===
                    audio_url = None
                    if self.qqmusic_retry_on_ratelimit:
                        last_fetch_error = None
                        retry_sent = False
                        for attempt in range(1, self.qqmusic_retry_max_attempts + 1):
                            try:
                                audio_url = await qq_api.fetch_song_url(songmid, song_display_name)
                                if audio_url:
                                    break  # 获取成功
                                logger.warning(f"QQ音乐获取播放链接返回空，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                                if attempt < self.qqmusic_retry_max_attempts:
                                    # 第一次重试时通知群聊
                                    if not retry_sent:
                                        try:
                                            await event.send(event.plain_result(f"⏳ QQ音乐获取播放链接遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                            retry_sent = True
                                        except:
                                            pass
                                    await asyncio.sleep(5)
                            except Exception as e:
                                last_fetch_error = str(e)
                                logger.warning(f"QQ音乐获取播放链接异常: {e}，尝试 {attempt}/{self.qqmusic_retry_max_attempts}")
                                if attempt < self.qqmusic_retry_max_attempts:
                                    # 第一次重试时通知群聊
                                    if not retry_sent:
                                        try:
                                            await event.send(event.plain_result(f"⏳ QQ音乐获取播放链接遇到风控，正在自动重试...（最多重试{self.qqmusic_retry_max_attempts}次，每次间隔5秒）"))
                                            retry_sent = True
                                        except:
                                            pass
                                    await asyncio.sleep(5)
                        
                        if not audio_url:
                            error_detail = f" (最后一次错误: {last_fetch_error})" if last_fetch_error else ""
                            error_msg = f"❌ QQ音乐源获取失败: 无法获取《{song_display_name}》的播放链接。已重试{self.qqmusic_retry_max_attempts}次仍失败。可能原因：1) 该歌曲是VIP专享 2) QQ音乐API触发风控 3) 网络问题。建议换一首歌或稍后再试。{error_detail}"
                            logger.error(error_msg)
                            return error_msg
                    else:
                        audio_url = await qq_api.fetch_song_url(songmid, song_display_name)
                    
                    if not audio_url:
                        error_msg = f"❌ QQ音乐源获取失败: 无法获取《{song_display_name}》的播放链接。可能原因：1) 该歌曲是VIP专享 2) QQ音乐API限制 3) 网络问题。建议换一首歌或稍后再试。"
                        logger.error(error_msg)
                        return error_msg
                    
                    logger.info("QQ音乐: 开始流式下载音频")
                    await self._send_progress_notice(
                        event,
                        f"📥 正在下载 QQ 音乐《{song_display_name}》，下载完成后会自动提交 SoulX...",
                    )
                    temp_audio_file = await self._download_audio(audio_url, ".mp3")
                    if not temp_audio_file:
                        return "❌ QQ音乐下载失败或文件超过大小限制，请换一首歌或稍后再试。"
                    logger.info(f"QQ音乐: 下载完成，文件大小: {os.path.getsize(temp_audio_file)} bytes")
                    await self._send_progress_notice(
                        event,
                        f"✅ QQ 音乐下载完成（{os.path.getsize(temp_audio_file) / 1024 / 1024:.1f} MiB），正在提交中间层...",
                    )
                    
                    song_input_for_api = temp_audio_file
                    
                except Exception as e:
                    logger.error(f"QQ音乐下载失败: {traceback.format_exc()}")
                    return f"❌ QQ音乐源获取失败: {e}。建议换一首歌或稍后再试。"
                finally:
                    await qq_api.close()
            
            base_url = self._get_engine_base_url(api_type)
            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_connect | url={base_url}"
            )
            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_submit | api=/convert | "
                f"model={selected_model}"
            )
            result_path, cache_hit, effective_seed = await self._predict_cover(
                event=event,
                api_type=api_type,
                song_name_src=song_input_for_api,
                model_name=selected_model,
                key_shift=key_shift,
            )
            logger.info(f"[CoverFlow:{flow_id}] stage=backend_done")
            
            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=selected_song.get('name', song_name), cache_hit=cache_hit)
                if api_type == "svcvc" and effective_seed is not None:
                    await event.send(event.plain_result(
                        f"🎲 SoulX-SVCVC 本次实际种子：{effective_seed}"
                        f"（{'随机' if self.svcvc_random_seed else '固定'}）"
                    ))
                song_artist = selected_song.get('artists', '未知艺人')
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！已成功使用 {self._engine_display_name(api_type)} 音色【{selected_model}】发送《{selected_song['name']}》({song_artist}) 的翻唱版本！"
                else:
                    result_msg = f"已成功使用 {self._engine_display_name(api_type)} 音色【{selected_model}】生成《{selected_song['name']}》({song_artist}) 的翻唱版本！音频文件已发送。"
                if api_type != "svcvc" and key_shift != original_key_shift and self.enable_auto_key_shift:
                    gender_label = {"male": "男", "female": "女"}.get
                    result_msg += f"\n🎵 自动升降调：{song_artist}({gender_label(artist_gender, '?')}) → {model_display}({gender_label(model_gender, '?')})，音调 {original_key_shift:+d} → {key_shift:+d}"
            else:
                result_msg = "生成失败，后端未返回有效文件路径。"
                
            # === 方案E：记录偏好 ===
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                pref["usage_count"] += 1
                pref["last_used_time"] = datetime.now().isoformat()
                pref["last_used_model"] = selected_model
                pref["last_used_api_type"] = api_type
                
                song_entry = f"{selected_song['name']} - {selected_song['artists']}"
                existing = [s for s in pref["favorite_songs"] if s.split(" - ")[0] == selected_song['name']]
                if not existing:
                    pref["favorite_songs"].append(song_entry)
                    if len(pref["favorite_songs"]) > 30:
                        pref["favorite_songs"] = pref["favorite_songs"][-30:]
                
                artist = selected_song['artists'].split(',')[0].strip() if ',' in selected_song['artists'] else selected_song['artists']
                artists_dict = pref.get("preferred_artists", {})
                if artist not in artists_dict:
                    artists_dict[artist] = {"count": 0, "last_time": None, "model": None}
                artists_dict[artist]["count"] += 1
                artists_dict[artist]["last_time"] = datetime.now().isoformat()
                artists_dict[artist]["model"] = selected_model
                pref["preferred_artists"] = artists_dict
                
                artist_model_map = pref.get("artist_model_map", {})
                if artist not in artist_model_map:
                    artist_model_map[artist] = {}
                model_key = selected_model
                if model_key not in artist_model_map[artist]:
                    artist_model_map[artist][model_key] = 0
                artist_model_map[artist][model_key] += 1
                pref["artist_model_map"] = artist_model_map
                
                await self._save_preferences()
            
            return result_msg
                
        except Exception as e:
            logger.error(f"[CoverFlow:{flow_id}] stage=failed\n{traceback.format_exc()}")
            if "Timeout" in str(e):
                return f"生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。"
            else:
                return f"{api_type.upper()} 翻唱时发生错误: {e}"
        finally:
            if temporary_search_api is not None:
                try:
                    await temporary_search_api.close()
                except Exception as close_error:
                    logger.debug(f"关闭临时 QQ 音乐客户端失败: {close_error}")
            try:
                if self.enable_send_file and result_path and os.path.isfile(result_path):
                    await asyncio.sleep(3)
                if result_path and os.path.isfile(result_path):
                    os.remove(result_path)
                if temp_audio_file and os.path.isfile(temp_audio_file):
                    os.remove(temp_audio_file)
            except OSError as cleanup_error:
                logger.debug(
                    f"删除临时文件失败（文件可能仍被占用，将在退出时清理）: {cleanup_error}"
                )

    @filter.llm_tool(name="get_available_models")
    async def get_available_models(self, event: AstrMessageEvent, api_type: str = "all") -> str:
        '''获取当前可用的 RVC、SVC-Fusion 与 SoulX-SVCVC 模型/参考音色列表。

        Args:
            api_type(string): 'rvc'、'svc'、'svcvc' 或 'all'，默认为'all'
        
        Returns:
            可用模型的详细信息列表（包含序号、显示名、文件名、别名）
        
        重要提示：
            - 可以直接用模型名称或别名指定模型，无需记住序号
            - 例如：model_name='塔菲' 会自动匹配到 tafeim.pth
            - 支持模糊匹配：输入部分名称也能找到
            - 只会显示已启用的引擎（enable_rvc / enable_svc / enable_svcvc）
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

            if api_type.lower() in ["all", "svcvc"] and self.enable_svcvc:
                svcvc_profiles = self.get_models_detailed_list(api_type="svcvc")
                if svcvc_profiles:
                    lines = []
                    for profile in svcvc_profiles:
                        alias_info = f" (别名: {profile['alias']})" if profile['alias'] else ""
                        lines.append(
                            f"  {profile['index']}. {profile['display']}"
                            f"【{profile['name']}】{alias_info}"
                        )
                    result_parts.append(
                        f"\n【SoulX-SVCVC 参考音色】(共 {len(svcvc_profiles)} 个)\n"
                        + "\n".join(lines)
                    )
                else:
                    result_parts.append(
                        "\n【SoulX-SVCVC 参考音色】暂无可用音色，请使用 /刷新svcvc音色"
                    )
            
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
            if self.enable_svcvc:
                status_parts.append("SoulX-SVCVC ✅")
            else:
                status_parts.append("SoulX-SVCVC ❌(已禁用)")
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
            api_type(string, optional): 翻唱类型，'rvc'、'svc' 或 'svcvc'。不指定则使用默认值或用户偏好
                当前可用引擎: {available_engines}
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认值或用户偏好
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。优先级高于model_index
                例如：'塔菲'会自动匹配到tafeim.pth，'maxmaxpoi'也能匹配
            key_shift(number, optional): 音调调整。RVC/SVC 范围 -12 到 12，SoulX-SVCVC 范围 -36 到 36；不指定则使用默认值或用户偏好。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。
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
            return "智能单步翻唱功能已在配置中禁用。请使用对应的独立翻唱工具。"
        
        try:
            available_engines = self._get_available_engines()
            
            if not available_engines:
                return "❌ 所有翻唱引擎均已禁用，请至少启用 RVC、SVC 或 SoulX-SVCVC 之一。"
            
            # === 方案B：检查是否有进行中的翻唱任务 ===
            active_task = self._get_active_task_status(event)
            if active_task:
                elapsed = int(time.time() - active_task.get("start_time", 0))
                return (
                    f"⏳ 您已经有翻唱任务在进行中啦nya～\n"
                    f"歌曲：《{active_task['song_name']}》\n"
                    f"引擎：{active_task['api_type'].upper()}\n"
                    f"状态：{active_task['status']}\n"
                    f"已进行：{elapsed}秒\n"
                    f"请稍等，完成后会自动发送音频喵！如果卡住了可以用 /取消翻唱任务 或 /查看翻唱任务 查看状态🐾"
                )
            
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
            
            actual_key_shift = (
                key_shift
                if key_shift is not None
                else (None if actual_api_type == "svcvc" else self.default_key_shift)
            )
            
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
                
                if key_shift is None:
                    actual_key_shift = (
                        None
                        if actual_api_type == "svcvc"
                        else pref.get("default_key_shift", self.default_key_shift)
                    )
                
                if artist_name and not model_name and not model_index:
                    artist_model_map = pref.get("artist_model_map", {})
                    if artist_name in artist_model_map:
                        model_counts = artist_model_map[artist_name]
                        if model_counts:
                            best_model = max(model_counts, key=model_counts.get)
                            models_info_tmp = self.get_models_detailed_list(actual_api_type)
                            for m in models_info_tmp:
                                if m["filename"] == best_model or m["display"] == best_model:
                                    actual_model_index = m["index"]
                                    break
            
            # 偏好加载后可能覆盖了 api_type，需要再次验证可用性
            if actual_api_type and actual_api_type.lower() not in available_engines:
                fallback = available_engines[0]
                return f"⚠️ {actual_api_type.upper()} 引擎已在配置中禁用。\n当前可用的翻唱引擎: {', '.join(available_engines).upper()}\n请指定可用的引擎，例如 api_type='{fallback}'"
            
            models_info = self.get_models_detailed_list(actual_api_type)
            actual_model_display = ""
            artist_pref_hint = ""
            for m in models_info:
                if m["index"] == actual_model_index:
                    actual_model_display = m["display"]
                    break
            
            if self.enable_preference_learning and artist_name and not model_name and not model_index:
                artist_model_map = pref.get("artist_model_map", {})
                if artist_name in artist_model_map:
                    model_counts = artist_model_map[artist_name]
                    best_model = max(model_counts, key=model_counts.get)
                    for m in models_info:
                        if (m["filename"] == best_model or m["display"] == best_model) and m["index"] == actual_model_index:
                            artist_pref_hint = f"\n💡 已自动为您选择翻唱{artist_name}时最常用的模型"
                            break
            
            source_display = "QQ音乐" if actual_music_source == "qqmusic" else ("网易云" if actual_music_source in ["netease", "netease_nodejs"] else actual_music_source)
            
            search_query = f"{song_name} {artist_name}" if artist_name else song_name
            
            # 注册活跃任务
            if not self._register_active_task(event, song_name, actual_api_type, actual_model_display):
                return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
            
            task = self._create_tracked_task(self._smart_cover_async(
                event, search_query, actual_api_type, actual_model_index, 
                actual_key_shift, actual_model_display, actual_music_source
            ))
            self._bind_active_asyncio_task(event, task)
            
            return f"🎵 正在从【{source_display}】用【{actual_model_display}】翻唱《{song_name}》... 请稍等，音频生成后会自动发送！{artist_pref_hint}"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"智能翻唱出错: {e}"

    async def _smart_cover_async(self, event: AstrMessageEvent, song_name: str,
                                 api_type: str, model_index: int, key_shift: Optional[int],
                                 model_display: str, music_source: str = None):
        """smart_cover 异步执行函数"""
        try:
            logger.info(
                f"[MatsukoCover任务Debug] 后台翻唱流程已进入 | song={song_name} | "
                f"api={api_type} | model={model_display} | source={music_source}"
            )
            result = await self._do_cover(event, song_name, api_type, model_index, key_shift, music_source)

            # 检查是否失败（返回字符串以❌开头、包含失败/错误/超时/未找到等关键词都表示失败）
            is_failed = (result.startswith("❌") or "失败" in result or "错误" in result 
                        or "超时" in result or "未找到" in result or "不存在" in result
                        or "无法获取" in result or "触发风控" in result
                        or "当前没有" in result or "已禁用" in result)

            # === 无论成功失败，都发送结果消息到群聊 ===
            if is_failed:
                # 失败时强制发送错误消息（不受 enable_config_report 开关限制）
                await event.send(event.plain_result(result))
            elif self.enable_config_report:
                # 成功时根据配置决定是否发送配置报告
                source_info = f", 音乐源={music_source}" if music_source else ""
                report_shift = self._effective_key_shift(api_type, key_shift)
                result += f"\n\n📊 本次配置：类型={self._engine_display_name(api_type)}, 模型={model_display}, 调音={report_shift:+d}{source_info}"
                await event.send(event.plain_result(result))

            # === LLM 通知机制（方案A+B整合）===
            cache_hit = "命中缓存" in result or "cache hit" in result.lower()
            if is_failed:
                await self._notify_llm_with_context(event, song_name, "failed", result_detail=result)
            elif cache_hit:
                await self._notify_llm_with_context(event, song_name, "cache_hit")
            else:
                await self._notify_llm_with_context(event, song_name, "success")

        except Exception as e:
            logger.error(f"smart_cover 异步执行失败: {traceback.format_exc()}")
            try:
                error_msg = f"❌ 翻唱失败: {e}"
                await event.send(event.plain_result(error_msg))
                await self._notify_llm_with_context(event, song_name, "failed", result_detail=str(e))
            except:
                pass
        
        finally:
            # 方案B：无论成功失败，都清理活跃任务
            self._finish_active_task(event)

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
            api_type(string, optional): 翻唱类型，'rvc'、'svc' 或 'svcvc'
            model_index(number, optional): 统一使用的模型序号
            key_shift(number, optional): 统一音调值。RVC/SVC 范围 -12 到 12，SoulX-SVCVC 范围 -36 到 36。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。
            music_source(string, optional): 音乐源选择，'netease'(网易云) 或 'qqmusic'(QQ音乐)
        
        Returns:
            任务确认信息（实际结果会异步推送到聊天中）
        '''
        if not self.enable_batch_cover:
            return "批量翻唱功能已在配置中禁用，请逐个使用 smart_cover。"
        
        available_engines = self._get_available_engines()
        
        if not available_engines:
            return "❌ 所有翻唱引擎均已禁用，请至少启用 RVC、SVC 或 SoulX-SVCVC 之一。"
        
        # === 方案B：检查是否有进行中的翻唱任务 ===
        active_task = self._get_active_task_status(event)
        if active_task:
            elapsed = int(time.time() - active_task.get("start_time", 0))
            return (
                f"⏳ 您已经有翻唱任务在进行中啦nya～\n"
                f"歌曲：《{active_task['song_name']}》\n"
                f"状态：{active_task['status']}\n"
                f"已进行：{elapsed}秒\n"
                f"请等当前任务完成后再发起批量翻唱喵！🐾"
            )
        
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
            actual_key_shift = (
                key_shift
                if key_shift is not None
                else (None if actual_api_type == "svcvc" else self.default_key_shift)
            )
            
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                if not api_type:
                    actual_api_type = pref.get("default_api_type", actual_api_type)
                if not model_index:
                    actual_model_index = pref.get("default_model_index", actual_model_index)
                if key_shift is None:
                    actual_key_shift = (
                        None
                        if actual_api_type == "svcvc"
                        else pref.get("default_key_shift", actual_key_shift)
                    )
            
            # 偏好加载后可能覆盖了 api_type，需要再次验证可用性
            if actual_api_type and actual_api_type.lower() not in available_engines:
                return f"⚠️ {actual_api_type.upper()} 引擎已在配置中禁用。\n当前可用的翻唱引擎: {', '.join(available_engines).upper()}\n请指定可用的引擎，例如 api_type='{available_engines[0]}'"
            
            actual_music_source = music_source or self.default_api
            if self.disable_netease and actual_music_source in ["netease", "netease_nodejs"]:
                actual_music_source = "qqmusic"
                
            # 注册活跃任务
            batch_song_names = "、".join(songs[:3])
            if len(songs) > 3:
                batch_song_names += f" 等{len(songs)}首"
            if not self._register_active_task(event, f"[批量]{batch_song_names}", actual_api_type):
                return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"
            
            # === 创建后台任务（异步执行）===
            task = self._create_tracked_task(
                self._execute_batch_cover_async(
                    event=event,
                    songs=songs,
                    api_type=actual_api_type,
                    model_index=actual_model_index,
                    key_shift=actual_key_shift,
                    music_source=actual_music_source
                )
            )
            self._bind_active_asyncio_task(event, task)
            
            # === 立即返回确认消息（不会被超时取消）===
            report_shift = self._effective_key_shift(actual_api_type, actual_key_shift)
            confirm_msg = (
                f"📦 已接收批量翻唱任务！共 {len(songs)} 首歌曲\n"
                f"⚙️ 配置：{self._engine_display_name(actual_api_type)} / 第{actual_model_index}个模型 / 音调{report_shift:+d}\n"
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
                                        key_shift: Optional[int],
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
                    is_failed = (
                        result.startswith("❌")
                        or any(marker in result for marker in (
                            "失败", "错误", "超时", "未找到", "不存在",
                            "无法获取", "触发风控", "当前没有", "已禁用",
                        ))
                    )
                    if is_failed:
                        results.append(
                            f"❌ [{idx}/{len(songs)}] 《{song_name}》：{result[:160]}"
                        )
                        fail_count += 1
                    else:
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
        finally:
            # 方案B：批量任务结束后清理活跃任务
            self._finish_active_task(event)

    # ==================== 方案E：偏好学习与推荐系统 ====================
    
    @filter.llm_tool(name="save_preference")
    async def save_preference(self, event: AstrMessageEvent,
                             preference_type: str,
                             value: str) -> str:
        '''保存用户的偏好设置 - 让AI记住你的喜好！

        可以保存常用的模型、音调等设置，下次翻唱时自动应用。
        
        Args:
            preference_type(string): 偏好类型：
                - 'api_type': 默认翻唱类型 ('rvc'、'svc' 或 'svcvc')
                - 'model_index': 默认模型序号 (数字字符串)
                - 'key_shift': 默认音调调整（RVC/SVC 为 -12到12，SoulX-SVCVC 为 -36到36）
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
                "api_type": {"type": str, "valid_values": ["rvc", "svc", "svcvc"]},
                "model_index": {"type": int, "min": 1},
                "key_shift": {"type": int, "min": -36, "max": 36}
            }
            
            if preference_type not in valid_types:
                return f"无效的偏好类型: {preference_type}\n支持的类型: {', '.join(valid_types.keys())}"
            
            type_info = valid_types[preference_type]
            target_type = type_info["type"]
            
            try:
                converted_value = target_type(value)
            except (ValueError, TypeError):
                return f"无法将值 '{value}' 转换为 {target_type.__name__} 类型"

            if preference_type == "key_shift":
                preferred_engine = pref.get("default_api_type", self.default_api_type)
                min_shift, max_shift = self._key_shift_range(preferred_engine)
                type_info = {**type_info, "min": min_shift, "max": max_shift}
            
            if "valid_values" in type_info and converted_value not in type_info["valid_values"]:
                return f"无效的值: {converted_value}，可选值: {type_info['valid_values']}"
            
            if "min" in type_info and converted_value < type_info["min"]:
                return f"值太小: {converted_value}，最小值为 {type_info['min']}"
            
            if "max" in type_info and converted_value > type_info["max"]:
                return f"值太大: {converted_value}，最大值为 {type_info['max']}"
            
            pref[f"default_{preference_type}"] = converted_value
            await self._save_preferences()
            
            type_display = {
                "api_type": f"默认翻唱类型 → {self._engine_display_name(converted_value)}",
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
            
            # === 偏好歌手（按频率排序）===
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                recommendation.append(f"\n🎤 您偏爱的歌手（按频率排序）：")
                for artist, info in sorted_artists[:5]:
                    model_info = f", 常用模型: {info.get('model', '未知')}" if info.get('model') else ""
                    recommendation.append(f"   - {artist} ({info.get('count', 0)}次{model_info})")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                recommendation.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    recommendation.append(f"   - {artist} → {best} ({models[best]}次)")
            
            # === 结合当前歌曲的建议 ===
            if song_name:
                recommendation.append(f"\n🎯 针对《{song_name}》的建议：")
                
                matched_artist = None
                for artist in preferred_artists:
                    if artist and artist in song_name:
                        matched_artist = artist
                        break
                
                if matched_artist:
                    recommendation.append(f"   ✨ 检测到这是您偏爱的歌手 {matched_artist} 的作品！")
                    if matched_artist in artist_model_map:
                        best_model = max(artist_model_map[matched_artist], key=artist_model_map[matched_artist].get)
                        recommendation.append(f"   💡 建议使用模型 {best_model}（您翻唱此歌手时最常用）")
                    else:
                        recommendation.append(f"   💡 建议使用您常用的配置进行翻唱。")
                
                song_names = [s.split(" - ")[0] for s in favorite_songs]
                if song_name in song_names:
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
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                stats.append(f"\n🎤 偏爱歌手（按频率排序）：")
                for artist, info in sorted_artists[:10]:
                    model_info = f", 常用: {info.get('model', '未知')}" if info.get('model') else ""
                    stats.append(f"   • {artist} - {info.get('count', 0)}次{model_info}")
            else:
                stats.append(f"\n🎤 偏爱歌手：暂无")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                stats.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    stats.append(f"   • {artist} → {best} ({models[best]}次)")
            
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
            await self._save_preferences()
            
            return "✅ 已清除您的所有翻唱历史和偏好数据！\n\n包括：\n• 使用统计\n• 偏好设置\n• 收藏歌曲\n• 偏爱歌手记录\n\n下次使用时将从零开始重新学习您的喜好。"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"清除历史时出错: {e}"

    # ==================== 本地音频翻唱 LLM 工具 ====================
    
    @filter.llm_tool(name="cover_absolute_path_audio")
    async def cover_absolute_path_audio(self, event: AstrMessageEvent,
                                         audio_path: str,
                                         model_name: Optional[str] = None,
                                         model_index: int = 1,
                                         api_type: Optional[str] = None,
                                         key_shift: Optional[int] = None) -> str:
        '''将指定绝对路径的音频文件进行AI翻唱/音色转换。
        
        当配合其他插件（如 ide_sandbox）获取到了本地文件的绝对路径，并且用户要求对其进行翻唱时使用此工具。
        注意：只能处理本机确实存在的音频文件路径。
        
        【关于音调调整】
        如果你不确定应该升调还是降调，请不要传入 key_shift 参数，插件会根据歌曲和模型的匹配关系自动推断最佳音调。
        只有当用户明确要求特定音调（如"升3调"、"降2调"）时，才需要传入 key_shift。
        
        Args:
            audio_path(string): 必须是音频文件的完整绝对路径（如 C:\\path\\to\\audio.mp3）
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。不指定则使用默认模型
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认模型
            api_type(string, optional): 翻唱类型，'rvc'、'svc' 或 'svcvc'。不指定则使用默认配置
            key_shift(number, optional): 音调调整。RVC/SVC 范围 -12 到 12，SoulX-SVCVC 范围 -36 到 36；不指定则使用默认配置。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。
        
        Returns:
            处理结果的描述信息（异步执行，完成后自动发送音频）
        '''
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"
            
        if not audio_path or not os.path.exists(audio_path) or not os.path.isfile(audio_path):
            return f"❌ 提供的文件路径无效或文件不存在: {audio_path}"
            
        if not self._is_audio_file(audio_path):
            return f"❌ 提供的文件不是受支持的音频格式: {audio_path}"
            
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            return f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"
            
        actual_api_type = str(api_type or self.default_api_type).lower()
        
        available_engines = self._get_available_engines()
        
        if actual_api_type not in available_engines:
            return f"⚠️ {actual_api_type.upper()} 引擎已禁用。当前可用: {', '.join(available_engines).upper()}"
        
        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name, actual_api_type)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event, actual_api_type)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"
        
        models_info = self.get_models_detailed_list(actual_api_type)
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break
        
        # key_shift 处理优先级：LLM 传入 → LLM自动升降调 → 本地推断 → 默认配置
        inferred_key_shift = None
        inference_source = ""
        if key_shift is not None:
            actual_key_shift = key_shift
            inference_source = "LLM传入"
        elif actual_api_type == "svcvc":
            actual_key_shift = None
            inference_source = "SoulX自动音域/插件配置"
        else:
            # Step 1: 优先用LLM判断歌曲性别和模型性别
            if self.enable_auto_key_shift:
                filename_base = os.path.splitext(os.path.basename(audio_path))[0]
                # 清理常见后缀：_EM, _cover, _RVC, (Live), [HQ] 等
                song_name = re.sub(r'[_\-\s]*(?:cover|rvc|svc|em|ai|remix|inst|伴奏|翻唱|完整版|live|现场|mv|hq|flac|mp3|wav| instrumental)?\s*$', '', filename_base, flags=re.I).strip()
                
                song_gender, model_gender = await self._detect_gender_pair(
                    song_name, actual_model_display, event, source_is_song=True
                )
                
                if song_gender and model_gender:
                    actual_key_shift = await self._calc_auto_key_shift(song_name, actual_model_display, 0, song_gender, model_gender, event)
                    inferred_key_shift = actual_key_shift
                    inference_source = "LLM推断"
                    logger.info(f"[MatsukoCover] LLM自动推断音调: key_shift={actual_key_shift} (歌曲: {song_name}, 模型: {actual_model_display})")
                else:
                    # LLM 判断失败，尝试本地推断兜底
                    inferred = self._infer_key_shift_from_filename(audio_path, actual_model_display)
                    if inferred is not None:
                        actual_key_shift = inferred
                        inferred_key_shift = inferred
                        inference_source = "本地推断"
                        logger.info(f"[MatsukoCover] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {actual_model_display})")
                    else:
                        actual_key_shift = self.default_key_shift
                        inference_source = "默认配置"
            else:
                # Step 2: 未启用自动升降调时，尝试本地推断
                inferred = self._infer_key_shift_from_filename(audio_path, actual_model_display)
                if inferred is not None:
                    actual_key_shift = inferred
                    inferred_key_shift = inferred
                    inference_source = "本地推断"
                    logger.info(f"[MatsukoCover] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {actual_model_display})")
                else:
                    actual_key_shift = self.default_key_shift
                    inference_source = "默认配置"
        
        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            api_type=actual_api_type, key_shift=actual_key_shift
        ))
        
        display_shift = self._effective_key_shift(actual_api_type, actual_key_shift)
        shift_info = f" | 升降调: {display_shift:+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"🎵 正在用【{actual_model_display}】({actual_api_type.upper()}) 处理文件 {os.path.basename(audio_path)}{shift_info}... 请稍候，处理完成后会自动发送！"

    @filter.llm_tool(name="cover_local_audio")
    async def cover_local_audio(self, event: AstrMessageEvent,
                                 model_name: Optional[str] = None,
                                 model_index: int = 1,
                                 api_type: Optional[str] = None,
                                 key_shift: Optional[int] = None) -> str:
        '''将用户发送的本地音频文件或语音消息进行AI翻唱/音色转换。
        
        当用户发送了语音或音频文件并要求"翻唱这个"、"把我的声音变成塔菲"、"修音"时使用此工具。
        
        【关于音调调整】
        如果你不确定应该升调还是降调，请不要传入 key_shift 参数，插件会根据歌曲和模型的匹配关系自动推断最佳音调。
        只有当用户明确要求特定音调（如"升3调"、"降2调"）时，才需要传入 key_shift。
        
        Args:
            model_name(string, optional): 模型名称或别名（支持模糊匹配）。不指定则使用默认模型
            model_index(number, optional): 模型序号（从1开始）。不指定则使用默认模型
            api_type(string, optional): 翻唱类型，'rvc'、'svc' 或 'svcvc'。不指定则使用默认配置
            key_shift(number, optional): 音调调整。RVC/SVC 范围 -12 到 12，SoulX-SVCVC 范围 -36 到 36；不指定则使用默认配置。
                调用此工具前，你应该分析原曲演唱者性别与目标模型音域的匹配关系（如周杰伦男声→塔菲喵女声）。但分析完成后请不要传入 key_shift，插件会根据分析结果自动应用配置中的升降调值（男→女用 male_to_female_shift，女→男用 female_to_male_shift）。只有当用户明确要求特定数值（如"升3调"）时才传入 key_shift。
        
        Returns:
            处理结果的描述信息（异步执行，完成后自动发送音频）
        '''
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"
        
        audio_path = await self._extract_audio_from_event(event)
        
        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]
            else:
                return "❌ 未检测到音频文件或语音消息。请用户先发送要处理的音频文件或语音，然后再调用此工具。"
        
        actual_api_type = str(api_type or self.default_api_type).lower()
        
        available_engines = self._get_available_engines()
        
        if actual_api_type not in available_engines:
            return f"⚠️ {actual_api_type.upper()} 引擎已禁用。当前可用: {', '.join(available_engines).upper()}"
        
        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name, actual_api_type)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event, actual_api_type)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"
        
        models_info = self.get_models_detailed_list(actual_api_type)
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break
        
        # key_shift 处理优先级：LLM 传入 → LLM自动升降调 → 本地推断 → 默认配置
        inferred_key_shift = None
        inference_source = ""
        if key_shift is not None:
            actual_key_shift = key_shift
            inference_source = "LLM传入"
        elif actual_api_type == "svcvc":
            actual_key_shift = None
            inference_source = "SoulX自动音域/插件配置"
        else:
            # Step 1: 优先用LLM判断歌曲性别和模型性别
            if self.enable_auto_key_shift:
                filename_base = os.path.splitext(os.path.basename(audio_path))[0]
                # 清理常见后缀：_EM, _cover, _RVC, (Live), [HQ] 等
                song_name = re.sub(r'[_\-\s]*(?:cover|rvc|svc|em|ai|remix|inst|伴奏|翻唱|完整版|live|现场|mv|hq|flac|mp3|wav| instrumental)?\s*$', '', filename_base, flags=re.I).strip()
                
                song_gender, model_gender = await self._detect_gender_pair(
                    song_name, actual_model_display, event, source_is_song=True
                )
                
                if song_gender and model_gender:
                    actual_key_shift = await self._calc_auto_key_shift(song_name, actual_model_display, 0, song_gender, model_gender, event)
                    inferred_key_shift = actual_key_shift
                    inference_source = "LLM推断"
                    logger.info(f"[MatsukoCover] LLM自动推断音调: key_shift={actual_key_shift} (歌曲: {song_name}, 模型: {actual_model_display})")
                else:
                    # LLM 判断失败，尝试本地推断兜底
                    inferred = self._infer_key_shift_from_filename(audio_path, actual_model_display)
                    if inferred is not None:
                        actual_key_shift = inferred
                        inferred_key_shift = inferred
                        inference_source = "本地推断"
                        logger.info(f"[MatsukoCover] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {actual_model_display})")
                    else:
                        actual_key_shift = self.default_key_shift
                        inference_source = "默认配置"
            else:
                # Step 2: 未启用自动升降调时，尝试本地推断
                inferred = self._infer_key_shift_from_filename(audio_path, actual_model_display)
                if inferred is not None:
                    actual_key_shift = inferred
                    inferred_key_shift = inferred
                    inference_source = "本地推断"
                    logger.info(f"[MatsukoCover] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {actual_model_display})")
                else:
                    actual_key_shift = self.default_key_shift
                    inference_source = "默认配置"
        
        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            api_type=actual_api_type, key_shift=actual_key_shift
        ))
        
        display_shift = self._effective_key_shift(actual_api_type, actual_key_shift)
        shift_info = f" | 升降调: {display_shift:+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"🎵 正在用【{actual_model_display}】({actual_api_type.upper()}) 处理您的音频{shift_info}... 请稍候，处理完成后会自动发送！"

    # ==================== 方案A+B：任务状态查询与取消 ====================
    
    @filter.llm_tool(name="get_task_status")
    async def get_task_status(self, event: AstrMessageEvent) -> str:
        '''查询当前是否有正在进行的翻唱任务。

        当用户问"我的翻唱好了吗"、"任务进行得怎么样了"或"有没有在翻唱"时使用此工具。
        
        Returns:
            当前翻唱任务的状态信息
        '''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"
        
        active_task = self._get_active_task_status(event)
        if not active_task:
            return "✅ 当前没有正在进行的翻唱任务nya～您可以随时发起新的翻唱请求喵！🐾"
        
        elapsed = int(time.time() - active_task.get("start_time", 0))
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
        
        return (
            f"⏳ 有翻唱任务正在进行中nya～\n"
            f"歌曲：《{active_task['song_name']}》\n"
            f"引擎：{active_task['api_type'].upper()}\n"
            f"模型：{active_task.get('model_display', '未知')}\n"
            f"状态：{active_task['status']}\n"
            f"已进行：{time_str}\n"
            f"请稍等，完成后会自动发送音频喵！如果卡住了可以用 cancel_cover_task 取消🐾"
        )

    @filter.llm_tool(name="cancel_cover_task")
    async def cancel_cover_task(self, event: AstrMessageEvent) -> str:
        '''取消当前正在进行的翻唱任务。

        当用户说"取消翻唱"、"不唱了"、"停下来"或任务卡住太久时使用此工具。
        
        Returns:
            取消操作的结果
        '''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"
        
        key = self._get_task_key(event)
        if key not in self._active_cover_tasks:
            return "✅ 当前没有正在进行的翻唱任务，无需取消nya～"
        
        task_info = self._active_cover_tasks.pop(key, None)
        gradio_job = task_info.get("gradio_job")
        asyncio_task = task_info.get("asyncio_task")
        if gradio_job is not None:
            try:
                await asyncio.to_thread(gradio_job.cancel)
            except Exception as exc:
                logger.warning(f"取消 Gradio 后端任务失败: {exc}")
        if (
            isinstance(asyncio_task, asyncio.Task)
            and asyncio_task is not asyncio.current_task()
            and not asyncio_task.done()
        ):
            asyncio_task.cancel()
        return (
            f"🛑 已向插件和后端发送取消请求nya～\n"
            f"歌曲：《{task_info['song_name']}》\n"
            f"已进行：{int(time.time() - task_info['start_time'])}秒\n"
            f"您可以重新发起翻唱请求喵！🐾"
        )

    @filter.command("查看翻唱任务")
    async def view_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看翻唱任务'即可！")
            return
        result = await self.get_task_status(event)
        yield event.plain_result(result)

    @filter.command("取消翻唱任务")
    async def cancel_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'取消翻唱任务'即可！")
            return
        result = await self.cancel_cover_task(event)
        yield event.plain_result(result)

    # ==================== 辅助命令 ====================
    
    @filter.command("我的翻唱统计")
    async def view_stats_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看我的翻唱统计'即可！")
            return
        result = await self.view_my_stats(event)
        yield event.plain_result(result)
    
    # ==================== 本地音频翻唱：事件监听 ====================
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_with_audio(self, event: AstrMessageEvent):
        '''监听包含音频的消息，自动触发本地翻唱'''
        if not self.enable_local_audio_cover:
            return
        
        msg_text = event.message_str.strip()
        
        # 排除命令消息（让命令处理器自己处理）
        if msg_text.startswith("/") or msg_text.startswith("\\"):
            return
        
        # 始终尝试提取并缓存音频（无论是否有触发词，方便后续 /本地翻唱 命令使用）
        audio_path = await self._extract_audio_from_event(event)
        if audio_path:
            async with self._audio_cache_lock:
                self._recent_audio_cache[str(event.unified_msg_origin)] = (audio_path, time.time())
            await self._cleanup_expired_audio_cache()
            logger.info(f"[LocalAudio] 已缓存音频: {audio_path}")
        
        # 检查是否触发自动翻唱
        if not self.local_audio_auto_trigger:
            return
        
        # 检查消息文字是否包含触发关键词
        has_trigger = any(kw in msg_text for kw in self.local_audio_trigger_keywords)
        
        # 如果文字没有触发，检查文件名是否包含触发关键词（如上传"处理.mp3"）
        if not has_trigger:
            for comp in getattr(event.message_obj, "message", []):
                if isinstance(comp, (File, Record)):
                    file_name = getattr(comp, "name", "") or ""
                    if any(kw in file_name for kw in self.local_audio_trigger_keywords):
                        has_trigger = True
                        logger.info(f"[LocalAudio] 文件名 '{file_name}' 匹配触发关键词")
                        break
        
        if not has_trigger:
            return
        
        if not audio_path:
            logger.info("[LocalAudio] 消息含触发词但无音频，跳过")
            return
        
        # 解析参数
        words = msg_text.replace("，", " ").replace(",", " ").replace("。", " ").split()
        model_name = self.local_audio_default_model if self.local_audio_default_model else None
        api_type = self.default_api_type
        key_shift = self.local_audio_default_shift
        key_shift_specified = False
        
        for w in words:
            if w.lstrip('-').isdigit() and -36 <= int(w) <= 36:
                key_shift = int(w)
                key_shift_specified = True
            elif w.lower() in ("rvc", "svc", "svcvc"):
                api_type = w.lower()
            elif w not in self.local_audio_trigger_keywords and len(w) >= 2:
                model_name = w
        
        # 引擎可用性检查与回退
        if not self._is_engine_enabled(api_type):
            available_engines = self._get_available_engines()
            api_type = available_engines[0] if available_engines else None
        
        if not api_type:
            return

        # SoulX-SVCVC has its own configured pitch.  Do not accidentally
        # override it with the legacy local-audio RVC/SVC default unless the
        # user explicitly supplied a numeric shift in this message.
        if api_type == "svcvc" and not key_shift_specified:
            key_shift = None
        
        # 验证模型名，不匹配则使用默认
        if model_name:
            matched = self._find_model_index_by_name(model_name, api_type)
            if not matched:
                model_name = None
        
        # 后台执行，不阻塞事件传播
        self._create_tracked_task(self._execute_local_cover(
            event, audio_path, model_name, api_type=api_type, key_shift=key_shift
        ))
    
    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("[MatsukoCover] 正在清理资源...")
        
        # 取消所有待处理的任务
        if hasattr(self, '_pending_tasks') and self._pending_tasks:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
            self._pending_tasks.clear()
        
        # 保存偏好数据
        try:
            if hasattr(self, '_pref_lock') and self.enable_preference_learning:
                await self._save_preferences()
        except Exception as e:
            logger.error(f"[MatsukoCover] 卸载时保存偏好失败: {e}")
        
        # 清理API session
        try:
            if hasattr(self, 'api') and self.api:
                await self.api.close()
        except Exception as e:
            logger.debug(f"[MatsukoCover] 关闭API session: {e}")
        
        logger.info("[MatsukoCover] 资源清理完成")
