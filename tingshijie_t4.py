# coding=utf-8
#!/usr/bin/env python3
# -*- 听书世界(275听书/拾音盘) TVBox py 爬虫 (hipy T4/T3) -*-
"""
原理 (逆向自 拾音盘APP com.shiyinpan.app 与 TingShijie java 爬虫):
1. 275听书官方接口 (app.365ting.com/listen/Apitzg2025/) 书单/章节信息正常,
   但其播放地址接口 AppGetChapterUrl2023 已损坏 (所有章节返回同一占位音频,
   网页版 api_chapter.php 同样返回 url:null), 这就是原 java 版
   "获取不到播放地址/起播不成功" 的根因。
2. 拾音盘APP (275官方配套播放器) 使用另一套接口:
   - 搜索:  GET  https://api.365ting.com/tingyou-app/search?keyword=xx
   - 提取:  POST https://api.365ting.com/tingyou-app/import  {user_id, album_id}
            -> task_id -> GET import/status?task_id= 轮询 (热门书服务端有克隆缓存,秒回)
   - 曲目:  GET  https://api.365ting.com/api/albums/{platform_album_id}/tracks?user_id=
   - 播放:  GET  https://api.365ting.com/tingyou/stream?album={tingyou_id}&idx={序号}
            (匿名直连可用, 支持 Range 拖动)
3. 设备登录: POST /api/auth/device-login {device_id} 免注册, 同 device_id 恒定同 user_id。

本爬虫: 分类/详情元数据走 275 官方接口, 播放走拾音盘 stream 直链;
275 bookId 与拾音盘专辑 id 体系不同, 详情时自动按"书名+演播"匹配拾音盘专辑。

TVBox 配置示例 (hipy-server T4):
{
  "key": "hipy_tingshijie", "name": "听书世界", "type": 4,
  "api": "http://127.0.0.1:5707/api/v1/vod/听书世界",
  "searchable": 1, "quickSearch": 1, "filterable": 0
}
T3 内置 python 的 app: {"key":"hipy_tingshijie_t3","name":"听书世界","type":3,"api":"./听书世界.py"}
"""

import re
import time
import uuid

import requests

try:
    from base.spider import BaseSpider
except ImportError:
    try:
        from t4.base.spider import BaseSpider
    except ImportError:
        BaseSpider = object  # 兜底, 保证脚本可独立加载

UA = 'TingShiJie/1.8.8 (m.i275.com)'
APP_UA = 'okhttp/4.9.3'
SIGN_KEY = 'J9gSpfUlzYxE8Hn5IXiGaD2jVMrwAm0K'
DEFAULT_BASE = 'https://app.365ting.com/listen/Apitzg2025/'
CONFIG_URLS = ['http://101.43.48.231:8090/config/tingchina2025.txt']
API = 'https://api.365ting.com'
TY = API + '/tingyou-app'
PLAY_FLAG = '拾音盘'
PAGE_SIZE = 120
DEVICE_PREFIX = 'tvbox-tingshijie-'

CLASSES = [
    ('6', '玄幻奇幻'), ('7', '都市言情'), ('8', '宫斗女频'), ('9', '官场商战'),
    ('10', '武侠仙侠'), ('11', '刑侦推理'), ('12', '探险科幻'), ('13', '重生穿越'),
    ('14', '恐怖惊悚'), ('15', '文学历史'), ('49', '两性情感'),
]


class Spider(BaseSpider):

    def getName(self):
        return '听书世界'

    def init(self, extend=''):
        self._app = requests.Session()
        self._app.headers.update({'User-Agent': APP_UA, 'Content-Type': 'application/json'})
        # 275 官方书单接口: 校验配置分发地址, 失效则回退默认 (修复原 java 版致命 bug)
        self.base = self._resolveBase()
        self._uid = None
        self._album_map = {}    # 275 bookId -> 拾音盘专辑 id
        self._info_cache = {}   # 拾音盘专辑 id -> {platform_id,title,cover,desc,teller,tracks}
        self._ty_meta = {}      # 拾音盘专辑 id -> 搜索结果元数据 (标题兜底)

    # ==================== 初始化辅助 ====================
    def _resolveBase(self):
        candidates = []
        for u in CONFIG_URLS:
            try:
                text = requests.get(u, timeout=3, headers={'User-Agent': UA}).text.strip()
                if text and len(text) <= 200 and re.match(r'^https?://[^\s"\'<>]+$', text):
                    candidates.append(text if text.endswith('/') else text + '/')
            except Exception:
                pass
        candidates.append(DEFAULT_BASE)
        for c in candidates:
            try:
                r = requests.get(c + 'appHomeByCategory?categoryId=6&page=1&size=1',
                                 timeout=4, headers={'User-Agent': UA})
                if r.json().get('status') == 0:
                    return c
            except Exception:
                continue
        return DEFAULT_BASE

    def _uidGet(self):
        if not self._uid:
            j = self._app.post(API + '/api/auth/device-login',
                               json={'device_id': DEVICE_PREFIX + uuid.uuid4().hex[:12]},
                               timeout=15).json()
            self._uid = j['data']['user_id']
        return self._uid

    # ==================== 275 官方书单接口 ====================
    def _api275(self, path, timeout=10):
        r = requests.get(self.base + path, timeout=timeout, headers={'User-Agent': UA})
        r.raise_for_status()
        return r.json()

    def _item(self, x):
        return {
            'vod_id': '275_' + str(x.get('id') or ''),
            'vod_name': x.get('bookTitle') or '',
            'vod_pic': x.get('bookImage') or '',
            'vod_remarks': x.get('bookAnchor') or '',
        }

    # ==================== 拾音盘接口 ====================
    def _tySearch(self, keyword):
        r = self._app.get(TY + '/search', params={'keyword': keyword}, timeout=15)
        r.raise_for_status()
        items = (r.json().get('data') or {}).get('items') or []
        for it in items:
            if it.get('id'):
                self._ty_meta[str(it['id'])] = it
        return items

    def _tyMatch(self, title, teller):
        items = self._tySearch(title.strip()) or []
        title = re.sub(r'\s+', '', title)
        for it in items:  # 演播者匹配优先
            if teller and teller in (it.get('teller') or ''):
                return it
        for it in items:  # 标题匹配
            t = re.sub(r'\s+', '', it.get('title') or '')
            if t.startswith(title) or title in t:
                return it
        return items[0] if items else None

    def _ensureAlbum(self, ty_id):
        """返回 {platform_id, title, cover, desc, teller, tracks}, 带缓存"""
        if ty_id in self._info_cache:
            return self._info_cache[ty_id]
        uid = self._uidGet()
        plat = None
        try:  # 已提取过的直接复用 (import 重复提交会再建一份歌单)
            r = self._app.get(API + '/api/users/%d/albums' % uid, timeout=15).json()
            for a in r.get('data') or []:
                if str(a.get('external_id')) == str(ty_id):
                    plat = a.get('id')
                    meta = a
                    break
        except Exception:
            meta = {}
        if plat is None:  # 提交提取任务
            r = self._app.post(TY + '/import', json={'user_id': uid, 'album_id': str(ty_id)},
                               timeout=30)
            task = (r.json().get('data') or {}).get('task_id')
            plat, meta = None, {}
            for _ in range(20):  # 热门书命中服务端克隆缓存秒回, 新书需等服务端抓取
                time.sleep(2)
                st = self._app.get(TY + '/import/status', params={'task_id': task},
                                   timeout=15).json().get('data') or {}
                if st.get('status') == 'done':
                    plat = st.get('album_id')
                    break
                if st.get('status') == 'failed':
                    break
            if plat is None:
                raise RuntimeError('提取未完成, 稍后重试')
            try:
                r = self._app.get(API + '/api/users/%d/albums' % uid, timeout=15).json()
                for a in r.get('data') or []:
                    if str(a.get('external_id')) == str(ty_id):
                        meta = a
                        break
            except Exception:
                pass
        r = self._app.get(API + '/api/albums/%s/tracks' % plat, params={'user_id': uid},
                          timeout=20).json()
        tracks = r.get('data') or []
        if not isinstance(tracks, list):
            tracks = tracks.get('tracks') or tracks.get('list') or []
        cached = self._ty_meta.get(str(ty_id)) or {}
        info = {
            'platform_id': plat, 'ty_id': str(ty_id),
            'title': meta.get('title') or cached.get('title') or '',
            'cover': meta.get('cover_url') or cached.get('cover_url') or '',
            'desc': meta.get('description') or '',
            'teller': cached.get('teller') or '',
            'tracks': tracks,
        }
        self._info_cache[ty_id] = info
        return info

    def _bookToAlbum(self, bid):
        if bid in self._album_map:
            return self._ensureAlbum(self._album_map[bid])
        book = self._api275('book?bookId=' + bid).get('data', {}).get('bookData', {})
        if not book:
            return None
        hit = self._tyMatch(book.get('bookTitle', ''), book.get('bookAnchor') or '')
        if not hit:
            return None
        ty_id = str(hit['id'])
        self._album_map[bid] = ty_id
        info = self._ensureAlbum(ty_id)
        # 275 侧的书名/封面/简介更干净, 优先使用
        info['title'] = book.get('bookTitle') or info['title']
        info['cover'] = book.get('bookImage') or info['cover']
        info['desc'] = book.get('bookDesc') or info['desc']
        info['teller'] = book.get('bookAnchor') or ''
        return info

    # ==================== 分类 ====================
    def homeContent(self, filter):
        return {'class': [{'type_id': tid, 'type_name': name} for tid, name in CLASSES],
                'filters': {}}

    def homeVideoContent(self):
        return {'list': []}

    def categoryContent(self, tid, pg, filter, extend):
        pg = int(pg or 1)
        result = {'list': [], 'page': pg, 'pagecount': pg, 'limit': PAGE_SIZE, 'total': 0}
        try:
            data = self._api275('appHomeByCategory?categoryId=%s&page=%d&size=%d'
                                % (tid, pg, PAGE_SIZE)).get('data') or []
            result['list'] = [self._item(x) for x in data]
            if len(result['list']) >= PAGE_SIZE:
                result['pagecount'] = pg + 1
            result['total'] = result['pagecount'] * PAGE_SIZE
        except Exception:
            pass
        return result

    # ==================== 详情 ====================
    def detailContent(self, ids):
        raw = str(ids[0]).strip()
        try:
            if raw.startswith('ty_'):
                info = self._ensureAlbum(raw[3:])
            else:
                info = self._bookToAlbum(raw.replace('275_', ''))
            if not info or not info.get('tracks'):
                return {'list': []}
            eps = []
            for i, t in enumerate(info['tracks']):
                idx = t.get('track_no') or i + 1
                eps.append('%s$%s|%s' % (t.get('title') or ('第%d集' % idx),
                                         info['ty_id'], idx))
            vod = {
                'vod_id': raw,
                'vod_name': info['title'],
                'vod_pic': info['cover'],
                'vod_remarks': info['teller'],
                'vod_content': info['desc'],
                'vod_play_from': PLAY_FLAG,
                'vod_play_url': '#'.join(eps),
            }
            return {'list': [vod]}
        except Exception:
            return {'list': []}

    # ==================== 搜索 ====================
    def searchContent(self, key, quick, pg='1'):
        result = {'list': [], 'page': 1, 'pagecount': 1, 'limit': 20, 'total': 0}
        try:
            items = self._tySearch(key) or []
            for it in items:
                result['list'].append({
                    'vod_id': 'ty_' + str(it.get('id') or ''),
                    'vod_name': it.get('title') or '',
                    'vod_pic': it.get('cover_url') or '',
                    'vod_remarks': '%s·%s集' % (it.get('teller') or it.get('author') or '',
                                                it.get('count') or '?'),
                })
            result['total'] = len(result['list'])
        except Exception:
            pass
        return result

    def searchContentPage(self, key, quick, pg='1'):
        return self.searchContent(key, quick, pg)

    # ==================== 播放 ====================
    def playerContent(self, flag, id, vipFlags):
        url = ''
        try:
            ty_id, idx = str(id).split('|', 1)
            url = '%s/tingyou/stream?album=%s&idx=%s' % (API, ty_id, idx)
            r = self._app.get(url, stream=True, timeout=15)
            ok = r.status_code in (200, 206) and 'audio' in (r.headers.get('Content-Type') or '')
            r.close()
            if not ok:
                url = ''
        except Exception:
            url = ''
        return {'parse': 0, 'playUrl': '', 'url': url, 'header': {'User-Agent': APP_UA}}

    # ==================== 其他 ====================
    def localProxy(self, params):
        return [200, 'text/plain', '', '']

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False
