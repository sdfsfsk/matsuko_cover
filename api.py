import json
import aiohttp
from astrbot import logger

try:
    from qqmusic_api import Client as QMClient
    from qqmusic_api.modules.search import SearchType
    QQ_MUSIC_AVAILABLE = True
except ImportError:
    QQ_MUSIC_AVAILABLE = False
    logger.warning("qqmusic-api-python not installed, QQ Music features disabled")

# 偷来的key
PARAMS = "D33zyir4L/58v1qGPcIPjSee79KCzxBIBy507IYDB8EL7jEnp41aDIqpHBhowfQ6iT1Xoka8jD+0p44nRKNKUA0dv+n5RWPOO57dZLVrd+T1J/sNrTdzUhdHhoKRIgegVcXYjYu+CshdtCBe6WEJozBRlaHyLeJtGrABfMOEb4PqgI3h/uELC82S05NtewlbLZ3TOR/TIIhNV6hVTtqHDVHjkekrvEmJzT5pk1UY6r0="
ENC_SEC_KEY = "45c8bcb07e69c6b545d3045559bd300db897509b8720ee2b45a72bf2d3b216ddc77fb10daec4ca54b466f2da1ffac1e67e245fea9d842589dc402b92b262d3495b12165a721aed880bf09a0a99ff94c959d04e49085dc21c78bbbe8e3331827c0ef0035519e89f097511065643120cbc478f9c0af96400ba4649265781fc9079"


class NetEaseMusicAPI:
    """
    网易云音乐API
    """

    def __init__(self):
        self.header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
        }
        self.headers = {"referer": "http://music.163.com"}
        self.cookies = {"appver": "2.0.2"}
        self._session = None
    
    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _request(
        self,
        url: str,
        data: dict = {},
        method: str = "GET",
    ):
        """统一请求接口"""
        if method.upper() == "POST":
            async with self.session.post(
                url, headers=self.header, cookies=self.cookies, data=data
            ) as response:
                if response.headers.get("Content-Type") == "application/json":
                    return await response.json()
                else:
                    return json.loads(await response.text())

        elif method.upper() == "GET":
            async with self.session.get(
                url, headers=self.headers, cookies=self.cookies
            ) as response:
                return await response.json()
        else:
            raise ValueError("不支持的请求方式")

    async def fetch_data(self, keyword: str, limit=5) -> list[dict]:
        """搜索歌曲"""
        url = "http://music.163.com/api/search/get/web"
        data = {"s": keyword, "limit": limit, "type": 1, "offset": 0}
        result = await self._request(url, data=data, method="POST")
        return [
            {
                "id": song["id"],
                "name": song["name"],
                "artists": "、".join(artist["name"] for artist in song["artists"]),
                "duration": song["duration"],
            }
            for song in result["result"]["songs"][:limit]
        ]

    async def fetch_comments(self, song_id: int):
        """获取热评"""
        url = f"https://music.163.com/weapi/v1/resource/hotcomments/R_SO_4_{song_id}?csrf_token="
        data = {
            "params": PARAMS,
            "encSecKey": ENC_SEC_KEY,
        }
        result = await self._request(url, data=data, method="POST")
        return result.get("hotComments", [])

    async def fetch_lyrics(self, song_id):
        """获取歌词"""
        url = f"https://netease-music.api.harisfox.com/lyric?id={song_id}"
        result = await self._request(url)
        return result.get("lrc", {}).get("lyric", "歌词未找到")

    async def fetch_extra(self, song_id: str | int) -> dict[str, str]:
        """
        获取额外信息
        """
        url = f"https://www.hhlqilongzhu.cn/api/dg_wyymusic.php?id={song_id}&br=7&type=json"
        result = await self._request(url)
        return {
            "title": result.get("title"),
            "author": result.get("singer"),
            "cover_url": result.get("cover"),
            "audio_url": result.get("music_url"),
        }
    async def close(self):
        await self.session.close()
class NetEaseMusicAPINodeJs:
    """
    网易云音乐API NodeJs版本
    """
    def __init__(self, base_url:str):
        # http://netease_cloud_music_api:{port}/
        self.base_url = base_url
        self._session = None
    
    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(self.base_url)
        return self._session

    async def _request(self, url: str, data: dict = {}, method: str = "GET"):
        if method.upper() == "POST":
            async with self.session.post(url, data=data) as response:
                if response.headers.get("Content-Type") == "application/json":
                    return await response.json()
                else:
                    return json.loads(await response.text())
        elif method.upper() == "GET":
            async with self.session.get(url) as response:
                return await response.json()
        else:
            raise ValueError("不支持的请求方式")


    async def fetch_data(self, keyword: str, limit=5) -> list[dict]:
        """搜索歌曲"""
        url = "/search"
        data = {"keywords": keyword, "limit": limit, "type": 1, "offset": 0}

        result = await self._request(url, data=data, method="POST")
        res = [
            {
                "id": song["id"],
                "name": song["name"],
                "artists": "、".join(artist["name"] for artist in song["artists"]),
                "duration": song["duration"],
            }
            for song in result["result"]["songs"][:limit]
        ]

        return res

    async def fetch_comments(self, song_id: int):
        """获取热评"""
        url = "/comment/hot"
        data = {
            "id": song_id,
            "type": 0,
        }
        result = await self._request(url, data=data, method="POST")
        return result.get("hotComments", [])

    async def fetch_lyrics(self, song_id):
        """获取歌词"""
        url = f"{self.base_url}/lyric?id={song_id}"
        result = await self._request(url)
        return result.get("lrc", {}).get("lyric", "歌词未找到")
    async def fetch_extra(self, song_id: str | int) -> dict[str, str]:
        """
        获取额外信息
        """
        url = "/song/url"
        data = {"id": song_id}
        result = await self._request(url, data=data, method="POST")
        return {
            "audio_url": result["data"][0].get("url", "")
        }
    async def close(self):
        await self.session.close()


class QQMusicAPI:
    """
    QQ音乐API (基于qqmusic-api-python库)
    
    支持功能：
    - 搜索歌曲
    - 获取歌曲详情
    - 获取播放链接
    """

    def __init__(self):
        """
        初始化QQ音乐API
        """
        if not QQ_MUSIC_AVAILABLE:
            raise ImportError("qqmusic-api-python not installed")
        self._client = None

    async def _get_client(self):
        """获取或创建客户端"""
        if self._client is None:
            self._client = QMClient()
            await self._client.__aenter__()
        return self._client

    async def fetch_data(self, keyword: str, limit: int = 5) -> list[dict]:
        """搜索歌曲
        
        Args:
            keyword: 搜索关键词
            limit: 返回数量限制
            
        Returns:
            歌曲列表，每首歌曲包含 id, name, artists, duration 等信息
        """
        try:
            client = await self._get_client()
            result = await client.search.search_by_type(
                keyword=keyword, 
                num=limit,
                search_type=SearchType.SONG
            )
            
            songs = []
            for song in result.song[:limit]:
                artists = "、".join(s.name for s in song.singer) if song.singer else "未知"
                songs.append({
                    "id": song.mid or str(song.id),
                    "name": song.name or song.title,
                    "artists": artists,
                    "duration": song.interval * 1000 if song.interval else 0,
                    "album": song.album.name if song.album else "",
                    "songmid": song.mid,
                })
            return songs
        except Exception as e:
            logger.error(f"QQ音乐搜索失败: {e}")
            return []

    async def fetch_song_detail(self, songmid: str) -> dict:
        """获取歌曲详情"""
        try:
            client = await self._get_client()
            result = await client.song.get_detail(songmid)
            track = result.track
            return {
                "name": track.name,
                "singer": [{"name": s.name} for s in track.singer],
                "album": {"name": track.album.name, "mid": track.album.mid},
                "interval": track.interval,
            }
        except Exception as e:
            logger.error(f"获取歌曲详情失败: {e}")
            return {}

    async def fetch_song_url(self, songmid: str, song_name: str = "", quality: str = "128") -> str:
        """获取歌曲播放链接（支持多种方式）
        
        Args:
            songmid: 歌曲ID (QQ音乐mid)
            song_name: 歌曲名称（用于备选API搜索）
            quality: 音质，可选 m4a/128/320/flac
            
        Returns:
            播放链接URL
        """
        # 方式1：使用官方API
        try:
            client = await self._get_client()
            result = await client.song.get_song_urls([songmid])
            if result.data and len(result.data) > 0:
                item = result.data[0]
                if item.purl and item.result == 0:
                    cdn_result = await client.song.get_cdn_dispatch()
                    if cdn_result.sip:
                        url = f"{cdn_result.sip[0]}{item.purl}"
                        logger.info(f"官方API获取成功: {songmid}")
                        return url
                elif item.result != 0:
                    logger.warning(f"官方API返回错误码 {item.result}，尝试备选API")
        except Exception as e:
            logger.error(f"官方API获取失败: {e}")
        
        # 方式2：使用第三方免费API（支持付费歌曲）
        try:
            import aiohttp
            import json
            search_keyword = song_name or songmid
            async with aiohttp.ClientSession() as session:
                # 使用第三方API搜索并获取播放链接 (需要apiKey)
                url = f"https://jkapi.com/api/music?plat=qq&type=json&apiKey=34accb9e82cc8ab5f8be9a59b8ab857c&name={search_keyword}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        data = json.loads(text)
                        if data.get("code") == 1 and data.get("music_url"):
                            logger.info(f"第三方API获取成功: {search_keyword}")
                            return data["music_url"]
        except Exception as e:
            logger.error(f"第三方API获取失败: {e}")
        
        # 方式3：使用另一个备选API
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                url = f"https://api.lolimi.cn/API/wy/api.php?msg={song_name or songmid}&n=1"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 200 and data.get("data") and data["data"].get("music"):
                            logger.info(f"备选API2获取成功: {song_name or songmid}")
                            return data["data"]["music"]
                        elif data.get("code") == 200 and data.get("music_url"):
                            logger.info(f"备选API2获取成功: {song_name or songmid}")
                            return data["music_url"]
        except Exception as e:
            logger.error(f"备选API2获取失败: {e}")
        
        return ""

    async def fetch_lyrics(self, songmid: str) -> str:
        """获取歌词"""
        try:
            client = await self._get_client()
            result = await client.song.get_lyric(songmid)
            return result.lyric or "歌词未找到"
        except Exception as e:
            logger.error(f"获取歌词失败: {e}")
            return "歌词未找到"

    async def fetch_extra(self, song_id: str | int) -> dict[str, str]:
        """获取额外信息（封面、音频URL等）"""
        try:
            detail = await self.fetch_song_detail(str(song_id))
            audio_url = await self.fetch_song_url(str(song_id))
            
            cover_url = ""
            if detail.get("album", {}).get("mid"):
                cover_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{detail['album']['mid']}.jpg"
            
            return {
                "title": detail.get("name", ""),
                "author": "、".join(s["name"] for s in detail.get("singer", [])) if detail.get("singer") else "",
                "cover_url": cover_url,
                "audio_url": audio_url,
            }
        except Exception as e:
            logger.error(f"获取额外信息失败: {e}")
            return {"title": "", "author": "", "cover_url": "", "audio_url": ""}

    async def close(self):
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._client = None


class MusicSearcher:
    """
    用于从指定音乐平台搜索歌曲信息的工具类。

    支持的平台：
    - qq: QQ 音乐
    - netease: 网易云音乐
    - kugou: 酷狗音乐
    - kuwo: 酷我音乐
    - baidu: 百度音乐
    - 1ting: 一听音乐
    - migu: 咪咕音乐
    - lizhi: 荔枝FM
    - qingting: 蜻蜓FM
    - ximalaya: 喜马拉雅
    - 5singyc: 5sing原创
    - 5singfc: 5sing翻唱
    - kg: 全民K歌

    支持的过滤条件：
    - name: 按歌曲名称搜索（默认）
    - id: 按歌曲 ID 搜索
    - url: 按音乐地址（URL）搜索
    """

    def __init__(self):
        """初始化请求 URL 和请求头"""
        self.base_url = "https://music.txqq.pro/"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        self._session = None
    
    @property
    def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    async def fetch_data(self, song_name: str, platform_type: str, limit: int = 5):
        """
        向音乐接口发送 POST 请求以获取歌曲数据

        :param song_name: 要搜索的歌曲名称
        :param platform_type: 音乐平台类型，如 'qq', 'netease' 等
        :return: 返回解析后的 JSON 数据或 None
        """
        data = {
            "input": song_name,
            "filter": "name",  # 当前固定为按名称搜索
            "type": platform_type,
            "page": 1,
        }

        try:
            async with self.session.post(
                self.base_url, data=data, headers=self.headers
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return [
                        {
                            "id": song["songid"],
                            "name": song.get("title", "未知"),
                            "artists": song.get("author", "未知"),
                            "url": song.get("url", "无"),
                            "link": song.get("link", "无"),
                            "lyrics": song.get("lrc", "无"),
                            "cover_url": song.get("pic", "无"),
                        }
                        for song in result["songs"][:limit]
                    ]
                else:
                    logger.error(f"请求失败:{response.status}")
                    return None
        except Exception as e:
            logger.error(f"请求异常: {e}")
            return None
    async def close(self):
        await self.session.close()
