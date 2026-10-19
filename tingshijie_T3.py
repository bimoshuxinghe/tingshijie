#!/usr/bin/python3
# -*- coding: utf-8 -*-
# @file    : 听书世界_T3.py  (参考 爬虫模板.min.py 模板编写)
"""
听书世界 (275听书 + 拾音盘) TVBox py 爬虫 —— T3 影视仓/内置python 运行时

原理 (逆向自 拾音盘APP com.shiyinpan.app 与 TingShijie java 爬虫):
1. 275听书官方接口 (app.365ting.com/listen/Apitzg2025/) 书单/搜索/详情正常,
   但其播放地址接口 AppGetChapterUrl2023 已损坏 (所有章节返回同一占位音频,
   网页版 api_chapter.php 同样返回 url:null) —— 原 java 版
   "获取不到播放地址/起播不成功" 的根因。
2. 拾音盘APP (275官方配套播放器) 使用另一套接口:
   - 搜索:  GET  https://api.365ting.com/tingyou-app/search?keyword=xx
   - 提取:  POST https://api.365ting.com/tingyou-app/import {user_id, album_id}
            -> task_id -> GET import/status?task_id= 轮询 (热门书有克隆缓存秒回)
   - 曲目:  GET  https://api.365ting.com/api/albums/{platform_album_id}/tracks?user_id=
   - 播放:  GET  https://api.365ting.com/tingyou/stream?album={tingyou_id}&idx={序号}
            匿名直连, 支持 Range 拖动
3. 设备登录: POST /api/auth/device-login {device_id} 免注册, 同 device_id 恒定同 user_id。

分类/首页/详情元数据走 275 官方接口, 播放走拾音盘 stream 直链;
275 bookId 与拾音盘专辑 id 体系不同, 详情时按"书名+演播"自动匹配拾音盘专辑。

详情提速 (对比上一版):
- device_id 固定落盘 -> 跨启动同一 user_id, 已提取专辑直接命中歌单, 跳过 import 轮询;
- 275->拾音盘 映射与专辑 platform_id 落盘 -> 重复打开直接拿曲目列表;
- 轮询"先查后睡"(0.4s) 替代固定 sleep(1) x2, 去掉提取完成后的多余歌单回查;
- 启动探活默认地址优先 (原分发地址已失效, 每次白等 3s 超时)。

配置示例 (type=3):
{
  "key": "tingshijie_t3", "name": "听书世界", "type": 3,
  "api": "./听书世界_T3.py",
  "searchable": 1, "quickSearch": 1, "filterable": 0
}
可选 ext: {"name":"自定义源名"} —— 不传则用默认名。
"""

import json
import os
import re
import sys
import tempfile
import time
import uuid

import requests as N

sys.path.append('..')
try:
    from base.spider import Spider as A          # 影视仓 / T3 内置 python
except Exception:
    try:
        from base.spider import BaseSpider as A  # hipy-server T4
    except Exception:
        try:
            from t4.base.spider import BaseSpider as A
        except Exception:
            class A(object):                     # 兜底: 独立加载不报错
                def __init__(self):
                    pass

UA = 'TingShiJie/1.8.8 (m.i275.com)'
APP_UA = 'okhttp/4.9.3'
DEFAULT_BASE = 'https://app.365ting.com/listen/Apitzg2025/'
CONFIG_URLS = ['http://101.43.48.231:8090/config/tingchina2025.txt']
API = 'https://api.365ting.com'
TY = API + '/tingyou-app'
PLAY_FLAG = '拾音盘'
PAGE_SIZE = 120
DEVICE_PREFIX = 'tvbox-tingshijie-'
# 落盘缓存: 固定 device_id + 专辑映射, 让重复打开详情页走快路径
CACHE_FILE = os.path.join(tempfile.gettempdir(), 'tvbox_tingshijie_cache.json')
META_KEYS = ('id', 'title', 'cover_url', 'teller', 'author', 'count', 'description')

CLASSES = [
    ('6', '玄幻奇幻'), ('7', '都市言情'), ('8', '宫斗女频'), ('9', '官场商战'),
    ('10', '武侠仙侠'), ('11', '刑侦推理'), ('12', '探险科幻'), ('13', '重生穿越'),
    ('14', '恐怖惊悚'), ('15', '文学历史'), ('49', '两性情感'),
]


def LOG(msg):
    try:
        print('[听书世界]', msg)
    except Exception:
        pass


class Spider(A):

    def __init__(self):
        super().__init__()
        self.name = '听书世界'
        self.error_play_url = ''
        self.extend = {}
        self._app = None
        self.base = DEFAULT_BASE
        self._uid = None
        self._device_id = ''
        self._album_map = {}    # 275 bookId -> 拾音盘专辑 id
        self._plat_map = {}     # 拾音盘专辑 id -> 平台歌单 id (已提取过)
        self._info_cache = {}   # 拾音盘专辑 id -> {tracks,...} 进程内缓存
        self._ty_meta = {}      # 拾音盘专辑 id -> 搜索结果元数据 (标题兜底)
        self._disk = None       # 落盘缓存 (懒加载)

    def getName(self):
        return self.name

    def init(self, extend='{}'):
        try:
            self.extend = json.loads(extend) if extend else {}
            if not isinstance(self.extend, dict):
                self.extend = {}
        except Exception as e:
            LOG('init extend 解析失败: %s' % e)
            self.extend = {}
        if self.extend.get('name'):
            self.name = self.extend.get('name')
        self._boot()

    # ==================== 启动 / 落盘缓存 ====================
    def _boot(self):
        """懒启动: 即使运行时没调 init 也不会挂"""
        if self._app is None:
            self._disk = self._loadDisk()
            self._device_id = self._disk.get('device_id') or ''
            self._album_map = dict(self._disk.get('album_map') or {})
            self._plat_map = dict(self._disk.get('plat_map') or {})
            self._ty_meta = dict(self._disk.get('meta') or {})
            self._app = N.Session()
            self._app.headers.update({'User-Agent': APP_UA, 'Content-Type': 'application/json'})
            self.base = self._resolveBase()
            LOG('base = %s (device=%s, 映射缓存 %d 条)'
                % (self.base, self._device_id or 'new', len(self._album_map)))

    @staticmethod
    def _loadDisk():
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
        return {}

    def _saveDisk(self):
        try:
            d = {
                'device_id': self._device_id or '',
                'album_map': self._album_map,
                'plat_map': self._plat_map,
                'meta': self._ty_meta,
            }
            tmp = CACHE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            LOG('cache 落盘失败: %s' % e)

    def _resolveBase(self):
        def probe(c):
            try:
                r = N.get(c + 'appHomeByCategory?categoryId=6&page=1&size=1',
                          timeout=4, headers={'User-Agent': UA})
                return r.json().get('status') == 0
            except Exception:
                return False
        # 默认地址直接探活; 分发服务器(常年失效)只在默认地址挂掉时才去查
        if probe(DEFAULT_BASE):
            return DEFAULT_BASE
        for u in CONFIG_URLS:
            try:
                text = N.get(u, timeout=2, headers={'User-Agent': UA}).text.strip()
                if text and len(text) <= 200 and re.match(r'^https?://[^\s"\'<>]+$', text):
                    c = text if text.endswith('/') else text + '/'
                    if probe(c):
                        return c
            except Exception:
                pass
        return DEFAULT_BASE

    def _uidGet(self):
        if not self._uid:
            if not self._device_id:  # 固定设备号: 跨启动同一账号, 已提取专辑直接命中
                self._device_id = DEVICE_PREFIX + uuid.uuid4().hex[:12]
                self._saveDisk()
            j = self._app.post(API + '/api/auth/device-login',
                               json={'device_id': self._device_id},
                               timeout=15).json()
            self._uid = j['data']['user_id']
        return self._uid

    # ==================== 275 官方书单接口 ====================
    def _api275(self, path, timeout=10):
        r = N.get(self.base + path, timeout=timeout, headers={'User-Agent': UA})
        r.raise_for_status()
        return r.json()

    def _item(self, x):
        return {
            'vod_id': '275_' + str(x.get('id') or ''),
            'vod_name': x.get('bookTitle') or '',
            'vod_pic': x.get('bookImage') or '',
            'vod_remarks': x.get('bookAnchor') or '',
        }

    def _listByCat(self, tid, pg, size=PAGE_SIZE):
        data = self._api275('appHomeByCategory?categoryId=%s&page=%d&size=%d'
                            % (tid, int(pg or 1), size)).get('data') or []
        return [self._item(x) for x in data]

    # ==================== 拾音盘接口 ====================
    def _tySearch(self, keyword):
        r = self._app.get(TY + '/search', params={'keyword': keyword}, timeout=15)
        r.raise_for_status()
        items = (r.json().get('data') or {}).get('items') or []
        added = False
        for it in items:
            if it.get('id'):
                k = str(it['id'])
                if k not in self._ty_meta:
                    added = True
                self._ty_meta[k] = {x: it.get(x) for x in META_KEYS}
        if added:
            self._saveDisk()
        return items

    def _tyMatch(self, title, teller):
        items = self._tySearch(title or '') or []
        title = re.sub(r'\s+', '', title or '')
        for it in items:  # 演播者匹配优先
            if teller and teller in (it.get('teller') or ''):
                return it
        for it in items:  # 标题匹配
            t = re.sub(r'\s+', '', it.get('title') or '')
            if t.startswith(title) or title in t:
                return it
        return items[0] if items else None

    def _albumMeta(self, uid, ty_id):
        """查用户歌单拿专辑元数据 (title/cover), 拿不到返回 {}"""
        try:
            r = self._app.get(API + '/api/users/%d/albums' % uid, timeout=15).json()
            for a in r.get('data') or []:
                if str(a.get('external_id')) == str(ty_id):
                    return a
        except Exception as e:
            LOG('用户歌单查询失败: %s' % e)
        return {}

    def _ensureAlbum(self, ty_id, _retried=False):
        """返回 {platform_id, title, cover, desc, teller, tracks}, 带缓存"""
        ty_id = str(ty_id)
        if ty_id in self._info_cache:
            return self._info_cache[ty_id]
        uid = self._uidGet()
        meta = {}
        plat = self._plat_map.get(ty_id)   # 落盘快路径: 该专辑已提取过
        if plat is None:                   # 兜底: 从歌单里找 (老缓存/别处提取的)
            meta = self._albumMeta(uid, ty_id)
            plat = meta.get('id')
        if plat is None:                   # 提交提取任务
            r = self._app.post(TY + '/import', json={'user_id': uid, 'album_id': ty_id},
                               timeout=30)
            task = (r.json().get('data') or {}).get('task_id')
            for i in range(30):            # 先查后睡: 热门书克隆缓存常秒回
                st = self._app.get(TY + '/import/status', params={'task_id': task},
                                   timeout=15).json().get('data') or {}
                if st.get('status') == 'done':
                    plat = st.get('album_id')
                    break
                if st.get('status') == 'failed':
                    break
                time.sleep(0.4 if i < 8 else 1)
            if plat is None:
                raise RuntimeError('提取未完成, 稍后重试')
            self._plat_map[ty_id] = plat
            self._saveDisk()
        if not meta and ty_id not in self._ty_meta:
            meta = self._albumMeta(uid, ty_id)  # 仅缺元数据时才回查 (历史记录直开)
        r = self._app.get(API + '/api/albums/%s/tracks' % plat, params={'user_id': uid},
                          timeout=20).json()
        tracks = r.get('data') or []
        if not isinstance(tracks, list):
            tracks = tracks.get('tracks') or tracks.get('list') or []
        if not tracks and not _retried:
            # 盘缓存的 plat 失效 (服务端重建歌单等): 重置缓存重新提取一次
            LOG('曲目为空, 重置提取缓存重试: %s' % ty_id)
            self._plat_map.pop(ty_id, None)
            self._info_cache.pop(ty_id, None)
            self._saveDisk()
            return self._ensureAlbum(ty_id, _retried=True)
        cached = self._ty_meta.get(ty_id) or {}
        info = {
            'platform_id': plat, 'ty_id': ty_id,
            'title': meta.get('title') or cached.get('title') or '',
            'cover': meta.get('cover_url') or cached.get('cover_url') or '',
            'desc': meta.get('description') or cached.get('description') or '',
            'teller': cached.get('teller') or '',
            'tracks': tracks,
        }
        if tracks:  # 空曲目不落缓存 (服务端还没抓好时, 下次进来还能再试)
            self._info_cache[ty_id] = info
        return info

    def _bookToAlbum(self, bid):
        if bid in self._album_map:
            return self._ensureAlbum(self._album_map[bid])
        book = self._api275('book?bookId=' + bid).get('data', {}).get('bookData', {})
        if not book or not book.get('bookTitle'):
            LOG('275 book 无数据: %s' % bid)
            return None
        hit = self._tyMatch(book.get('bookTitle', ''), book.get('bookAnchor') or '')
        if not hit:
            LOG('拾音盘未匹配到专辑: %s' % book.get('bookTitle'))
            return None
        ty_id = str(hit['id'])
        self._album_map[bid] = ty_id
        self._saveDisk()
        info = self._ensureAlbum(ty_id)
        # 275 侧的书名/封面/简介更干净, 优先使用
        info['title'] = book.get('bookTitle') or info['title']
        info['cover'] = book.get('bookImage') or info['cover']
        info['desc'] = book.get('bookDesc') or info['desc']
        info['teller'] = book.get('bookAnchor') or ''
        return info

    # ==================== 模板接口 ====================
    def homeContent(self, filter):
        S = {'class': [], 'filters': {}, 'list': [], 'parse': 0, 'jx': 0}
        try:
            self._boot()
            S['class'] = [{'type_id': tid, 'type_name': name} for tid, name in CLASSES]
            S['list'] = self._listByCat('6', 1, 12)  # 首页推荐 (玄幻热门)
        except Exception as e:
            LOG('homeContent 失败: %s' % e)
        return S

    def homeVideoContent(self):
        try:
            self._boot()
            return {'list': self._listByCat('6', 1, 12)}
        except Exception as e:
            LOG('homeVideoContent 失败: %s' % e)
            return {'list': []}

    def categoryContent(self, cid, page, filter, ext):
        S = {'list': [], 'page': int(page or 1), 'pagecount': int(page or 1),
             'limit': PAGE_SIZE, 'total': 0, 'parse': 0, 'jx': 0}
        try:
            self._boot()
            S['list'] = self._listByCat(cid, page)
            if len(S['list']) >= PAGE_SIZE:
                S['pagecount'] = int(page or 1) + 1
            S['total'] = S['pagecount'] * PAGE_SIZE
        except Exception as e:
            LOG('categoryContent 失败: %s' % e)
        return S

    def detailContent(self, did):
        raw = str(did[0]).strip()
        S = {'list': [], 'parse': 0, 'jx': 0}
        t0 = time.time()
        try:
            self._boot()
            if raw.startswith('ty_'):
                info = self._ensureAlbum(raw[3:])
            else:
                info = self._bookToAlbum(raw.replace('275_', ''))
            if not info or not info.get('tracks'):
                return S
            eps = []
            for i, t in enumerate(info['tracks']):
                idx = t.get('track_no') or i + 1
                eps.append('%s$%s|%s' % (t.get('title') or ('第%d集' % idx),
                                         info['ty_id'], idx))
            S['list'].append({
                'vod_id': raw,
                'vod_name': info['title'],
                'vod_pic': info['cover'],
                'vod_remarks': info['teller'],
                'vod_content': info['desc'],
                'vod_play_from': PLAY_FLAG,
                'vod_play_url': '#'.join(eps),
            })
            LOG('detail %s: %d集 %.2fs' % (raw, len(eps), time.time() - t0))
        except Exception as e:
            LOG('detailContent 失败 [%s] %.2fs: %s' % (raw, time.time() - t0, e))
        return S

    def searchContent(self, key, quick, page='1'):
        S = {'list': [], 'page': 1, 'pagecount': 1, 'limit': 20, 'total': 0,
             'parse': 0, 'jx': 0}
        try:
            self._boot()
            for it in self._tySearch(key) or []:
                S['list'].append({
                    'vod_id': 'ty_' + str(it.get('id') or ''),
                    'vod_name': it.get('title') or '',
                    'vod_pic': it.get('cover_url') or '',
                    'vod_remarks': '%s·%s集' % (it.get('teller') or it.get('author') or '',
                                                it.get('count') or '?'),
                })
            S['total'] = len(S['list'])
        except Exception as e:
            LOG('searchContent 失败 [%s]: %s' % (key, e))
        return S

    def playerContent(self, flag, pid, vipFlags):
        S = {'parse': 0, 'jx': 0, 'playUrl': '', 'url': '', 'header': {'User-Agent': APP_UA}}
        try:
            self._boot()
            ty_id, idx = str(pid).split('|', 1)
            url = '%s/tingyou/stream?album=%s&idx=%s' % (API, ty_id, idx)
            r = self._app.get(url, stream=True, timeout=15)
            ct = r.headers.get('Content-Type') or ''
            ok = r.status_code in (200, 206) and 'json' not in ct
            r.close()
            if ok:
                S['url'] = url
            else:
                LOG('playerContent 校验失败 http=%s ct=%s' % (r.status_code, ct))
        except Exception as e:
            LOG('playerContent 失败 [%s]: %s' % (pid, e))
        return S

    def localProxy(self, params):
        return 0

    def isVideoFormat(self, url):
        return False

    def manualVideoCheck(self):
        return False

    def destroy(self):
        try:
            self._app.close()
        except Exception:
            pass


if __name__ == '__main__':
    0
