import asyncio
import json
import re
import time
import traceback
import urllib.parse as parser

import httpx
import websockets
from loguru import logger

from configs import configs


class Browser(object):
    @classmethod
    async def connect(cls):
        browser = cls(configs['browser']['port'])
        if configs['browser']['verify'] and not await browser._verify():
            return None
        return browser

    def __init__(self, dev_port):
        self.port = dev_port

    async def _verify(self):  # 校验客户端和配置文件中的用户是否相同
        try:
            logger.info('[账号校验] | 正在校验账号...')
            page = await self.wait_for_page(r'https?://.*', require_bridge=False, timeout=60)
            user_info = json.loads((await page.eval('''
                (function () {
                    var xhr = new XMLHttpRequest()
                    xhr.open('POST', 'https://school.ismartlearning.cn/client/user/student-info', false)
                    xhr.withCredentials = true
                    xhr.send(null)
                    return xhr.responseText
                })()
            '''))['result']['value'])['data']
            spider_user = configs['user']['username']
            logger.debug(f'[账号校验] | 配置文件用户: {spider_user}')
            logger.debug(f'[账号校验] | 客户端用户: {json.dumps(user_info, indent=4)}')
            if str(spider_user) != user_info['mobile'] and str(spider_user) != user_info['username']:
                logger.warning('[账号校验] | 客户端中登录的账号与配置文件中账号不符')
                choice = input('[账号校验] | 继续使用可能会出现意料之外的问题，是否继续？[y/N]')
                if choice.lower() != 'y':
                    exit()
            else:
                logger.info('[账号校验] | 校验通过')
            return True
        except Exception:
            logger.warning(f'[账号校验] | 账号校验出错：\n{traceback.format_exc()}')

    async def _bridge_ready(self, page):
        """判断这个页面里有没有原生桥（NetBrowser.submitTask）。"""
        try:
            resp = await page.eval(
                'typeof NetBrowser !== "undefined" && typeof NetBrowser.submitTask === "function"'
            )
            return bool(resp.get('result', {}).get('value'))
        except Exception:
            return False

    async def wait_for_page(self, regexp=r'https?://.*', require_bridge=True, poll=2.0, timeout=300):
        """等待符合条件的页面。

        原实现把页面域名写死成 pc.ismartin.com，而该域名已经失效，
        这里改为：先按 URL 正则粗筛，再优先选带 courseId/bookId 的页面，
        最后用 NetBrowser 是否存在做终判——只要能拿到原生桥就能提交。
        """
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    pages = (await client.get(f'http://127.0.0.1:{self.port}/json')).json()
                except Exception:
                    pages = []

                candidates = [
                    p for p in pages
                    if 'webSocketDebuggerUrl' in p and re.fullmatch(regexp, p.get('url') or '')
                ]
                candidates.sort(
                    key=lambda p: 0 if ('courseId' in (p.get('url') or '') or 'bookId' in (p.get('url') or '')) else 1
                )

                for p in candidates:
                    page = Page(p['url'], p['webSocketDebuggerUrl'])
                    if not require_bridge:
                        return page
                    if await self._bridge_ready(page):
                        logger.info(f'[浏览器] | 已接管页面：{p["url"]}')
                        return page

                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f'等待可用页面超时（{timeout} 秒）。请确认客户端已打开并停留在课程页面，'
                        f'且快捷方式带 --remote-debugging-port={self.port}'
                    )
                await asyncio.sleep(poll)

    # noinspection PyTypeChecker
    async def get_current(self):
        async with httpx.AsyncClient(timeout=10) as client:
            pages = (await client.get(f'http://127.0.0.1:{self.port}/json')).json()
            for page in pages:
                params = dict(parser.parse_qsl(parser.urlsplit(page['url']).query))
                if 'courseId' not in params:
                    continue
                if 'bookId' in params:
                    return params['courseId'], params['bookId']
                return params['courseId'], None


class Page(object):
    def __init__(self, url, dev_url):
        self.id = 0
        self.url, self.dev_url = url, dev_url

    async def send(self, command, params):
        async with websockets.connect(self.dev_url, max_size=None) as devtools:
            await devtools.send(json.dumps({
                'id': self.id,
                'method': command,
                'params': params
            }))
            self.id += 1
            return json.loads(await devtools.recv())

    async def eval(self, script):
        result = await self.send(
            'Runtime.evaluate', {
                'expression': script,
                'awaitPromise': True
            }
        )
        return result['result']

    async def submit(self, book_id, chapter_id, task_id, score, seconds, percent, user_id):  # 提交任务点的得分
        model = 'NetBrowser.submitTask("%s", "%s", "%s", 0, "%d", %d, %d, "%s");'
        result = f'%7B%22studentid%22:{user_id},%22testInfo%22:%7B%22answerdata%22:%22%22,%22markdatao%22:%22%22%7D%7D'
        return await self.eval(
            model % (book_id, chapter_id, task_id, score, seconds, percent, result)
        )
