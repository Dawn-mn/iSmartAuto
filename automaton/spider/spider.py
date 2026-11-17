import asyncio
import httpx
import json
import random
import traceback
from hashlib import md5
from loguru import logger

from .captcha import recognize_ex

CAPTCHA_SALT = b'fa&s*l%$k!fq$k!ld@fjlk'
CAPTCHA_ATTEMPTS = 6          # 验证码单次识别率约 67%，6 次全错的概率约 0.1%


class LoginError(RuntimeError):
    """登录最终失败（已重试到上限）。"""


class Tree:  # 任务树
    def __init__(self, task):
        self.task = task
        self.child = []

    def sort(self):
        try:
            self.child.sort(
                key=lambda node: node.task['displayOrder']
            )
            for ch in self.child:
                ch.sort()
        except Exception:
            logger.warning(f'[构建任务树] | 排序出错：\n{traceback.format_exc()}')


class Spider(httpx.AsyncClient):
    def __init__(self):
        super().__init__(follow_redirects=True, timeout=httpx.Timeout(30.0))
        self.is_login = False

    async def _fetch_captcha(self):
        resp = await self.get('https://sso.ismartlearning.cn/captcha.html')
        if resp.status_code != 200:
            raise LoginError(f'验证码接口返回 {resp.status_code}')
        code, confidence = recognize_ex(resp.content)
        if not code or len(code) != 4:
            raise LoginError(f'验证码识别失败：{code!r}')
        return code

    async def login(self, username, password, attempts=CAPTCHA_ATTEMPTS):
        """登录，验证码识别错误时自动换一张重试。

        原实现只试一次且用 assert，识别错一次就整段抛异常，实测会有
        1/3 的概率直接失败。这里改为固定次数重试 + 明确异常。
        """
        if self.is_login:
            return {}

        hashed = md5(md5(str(password).encode()).hexdigest().encode() + CAPTCHA_SALT).hexdigest()
        last = {}

        for i in range(1, attempts + 1):
            try:
                self.cookies.clear()  # 重置 cookies
                logger.info(f'[登录] | 第 {i}/{attempts} 次尝试：获取验证码...')
                code = await self._fetch_captcha()

                info = (await self.post(
                    'https://sso.ismartlearning.cn/v2/tickets-v2',
                    data={
                        'username': str(username),
                        'password': hashed,
                        'captcha': code
                    },
                    headers={
                        'X-Requested-With': 'XMLHttpRequest',
                        'Origin': 'https://me.ismartlearning.cn',
                        'Referer': 'https://me.ismartlearning.cn/'
                    }
                )).json()['result']

                if info.get('code') == -26:
                    self.is_login = True
                    logger.success('[登录] | 登录成功')
                    return info

                last = info
                logger.warning(f'[登录] | 第 {i} 次失败：code={info.get("code")} msg={info.get("msg")}')
            except LoginError as e:
                last = {'code': None, 'msg': str(e)}
                logger.warning(f'[登录] | 第 {i} 次失败：{e}')
            except Exception:
                last = {'code': None, 'msg': '网络或解析异常'}
                logger.warning(f'[登录] | 第 {i} 次异常：\n{traceback.format_exc()}')

            await asyncio.sleep(random.uniform(0.8, 2.0))

        msg = f'[登录] | 连续 {attempts} 次登录失败，最后一次返回：{last}'
        logger.error(msg)
        raise LoginError(msg)

    async def get_courses(self):  # 获取课程列表
        try:
            logger.info('[获取课程列表] | 正在获取课程列表...')
            courses = (await self.post(
                'https://school.ismartlearning.cn/client/course/list-of-student?status=1',
                data={
                    'pager.currentPage': 1,
                    'pager.pageSize': 100
                }
            )).json()['data']['list']
            logger.debug(f"[获取课程列表] | {courses}")
            logger.success('[获取课程列表] | 获取课程列表成功')
            return courses
        except Exception:
            logger.warning(f'[获取课程列表] | 获取课程列表出错：\n{traceback.format_exc()}')

    async def get_books(self, course_id):  # 获取某课程的书籍列表
        try:
            await self.post(  # 必须有这个请求，否则后面会报错
                'https://school.ismartlearning.cn/client/course/list-of-student?status=1',
                data={
                    'pager.currentPage': 1,
                    'pager.pageSize': 100
                }
            )
            books = (await self.post(
                'https://school.ismartlearning.cn/client/course/textbook/list-of-student',
                data={
                    'courseId': course_id
                }
            )).json()['data']
            return books
        except Exception:
            logger.warning(f'[获取书籍列表] | 获取书籍列表出错：\n{traceback.format_exc()}')

    async def get_tasks(self, book_id, book_type, course_id):  # 获取某书籍的任务树
        try:
            logger.info('[获取任务列表] | 正在获取任务列表...')
            await self.post('https://school.ismartlearning.cn/client/course/textbook/chapters')
            tasks = (await self.post(
                'https://school.ismartlearning.cn/client/course/textbook/chapters',
                data={
                    'bookId': book_id,
                    'bookType': book_type,
                    'courseId': course_id
                }
            )).json()['data']
            id_record = {task['id']: Tree(task) for task in tasks}
            book_name = (await self.book_info(book_id))['bookName']
            root = Tree({
                'book_id': tasks[0]['book_id'],
                'unitStudyPercent': 0,
                'name': book_name
            })
            logger.info('[构建任务树] | 正在构建任务树...')
            for task_id in id_record:
                node = id_record[task_id]
                node_name = f'{node.task.get("name","")}[id:{node.task["id"]}]'
                # 2026 版接口给根节点也带 parent_id 字段，只是值为空串，
                # 所以不能再用 "键是否存在" 来区分根节点，必须看值。
                parent_id = node.task.get('parent_id') or node.task.get('parentId') or ''
                if parent_id:
                    if parent_id in id_record:
                        id_record[parent_id].child.append(node)
                    else:
                        logger.warning(f'[构建任务树] | {node_name} 父节点不存在')
                else:
                    root.child.append(node)
            root.sort()
            logger.success('[构建任务树] | 构建任务树完成')
            logger.success('[获取任务列表] | 获取任务列表完成')
            return root
        except Exception:
            logger.warning(f'[获取任务列表] | 获取任务列表出错：\n{traceback.format_exc()}')

    async def get_paper(self, paper_id, attempts=3):  # 获取任务点信息（包括题目和答案）
        for i in range(1, attempts + 1):
            try:
                logger.info(f'[获取任务点] | 正在获取任务点信息...（第 {i}/{attempts} 次）')
                ticket = (await self.post(
                    'https://sso.ismartlearning.cn/v1/serviceTicket',
                    data={
                        'service': 'https://xot-api.ismartlearning.cn/client/textbook/paperinfo'
                    }
                )).json()['data']['serverTicket']
                logger.debug(f'[获取任务点] | {ticket}')
                paper_info = (await self.post(
                    'https://xot-api.ismartlearning.cn/client/textbook/paperinfo',
                    params={
                        'ticket': ticket
                    },
                    data={
                        'paperId': paper_id
                    },
                    headers={
                        'Origin': 'https://me.ismartlearning.cn',
                        'Referer': 'https://me.ismartlearning.cn/',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept-Encoding': 'gzip, deflate'
                    }
                )).json()['data']
                # logger.debug(f'[获取任务点] | {json.dumps(paper_info, indent=4)}')
                logger.success('[获取任务点] | 获取任务点信息完成')
                return paper_info
            except Exception:
                logger.warning(f'[获取任务点] | 第 {i}/{attempts} 次出错：\n{traceback.format_exc()}')
                if i < attempts:
                    await asyncio.sleep(2 * i)
        logger.error('[获取任务点] | 重试耗尽，返回 None')
        return None

    async def user_info(self):
        try:
            logger.info('[获取用户信息] | 正在获取用户信息...')
            info = (await self.post('https://school.ismartlearning.cn/client/user/student-info')).json()
            logger.success('[获取用户信息] | 获取用户信息完成')
            return info
        except Exception:
            logger.warning(f'[获取用户信息] | 获取用户信息出错：\n{traceback.format_exc()}')

    async def book_info(self, book_id):
        try:
            logger.info('[获取书籍信息] | 正在获取书籍信息...')
            ticket = (await self.post(
                'https://sso.ismartlearning.cn/v1/serviceTicket',
                data={
                    'service': 'https://book-api.ismartlearning.cn/client/v2/book/info'
                }
            )).json()['data']['serverTicket']
            logger.debug(f'[获取书籍信息] | {ticket}')
            book_info = (await self.post(
                'https://book-api.ismartlearning.cn/client/v2/book/info',
                params={
                    'ticket': ticket
                },
                data={
                    'bookId': book_id,
                    'bookType': 0
                },
                headers={
                    'Origin': 'https://me.ismartlearning.cn',
                    'Referer': 'https://me.ismartlearning.cn/',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept-Encoding': 'gzip, deflate'
                }
            )).json()['data']
            logger.debug(f'[获取书籍信息] |  {json.dumps(book_info, indent=4)}')
            logger.success('[获取书籍信息] | 获取书籍信息完成')
            return book_info
        except Exception:
            logger.warning(f'[获取书籍信息] | 获取书籍信息出错：\n{traceback.format_exc()}')
