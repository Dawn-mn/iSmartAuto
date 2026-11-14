import json
import os
import time
import traceback

from bs4 import BeautifulSoup
from loguru import logger
from random import uniform

from configs import configs
from .devtools import Browser
from .pacing import Pacing
from .spider import Spider

PLACEHOLDER = ".   "
_paper_config = configs['paper']


def _ledger_path():
    """本地记账文件：记录已经提交过的任务点，用于断点续跑。

    平台不返回可靠的单任务完成标记（`unitStudyPercent` 对教材任务恒为 0），
    而书籍级 percent 有缓存延迟，靠它判断会重复提交。所以自己记账。
    """
    custom = os.environ.get('ISMART_LEDGER')
    if custom:
        return custom
    # 默认放在仓库外一层的工作区里（仓库目录本身在工作区沙箱下不可写）
    workspace = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(os.path.dirname(workspace), '.state', 'flashed.json')


def _load_ledger():
    try:
        with open(_ledger_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mark_done(task_id, name, seconds, score):
    path = _ledger_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = _load_ledger()
        data[task_id] = {
            'name': name,
            'seconds': seconds,
            'score': score,
            'at': int(time.time()),
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f'[记账] | 写入失败（不影响本次提交）：{e}')


async def list_books(detail):
    try:
        async with Spider() as spider:
            await spider.login(**configs['user'])
            courses = await spider.get_courses() or []

            for cr in courses:
                if detail:
                    hint = f'{cr["courseName"]}({cr.get("teacherName", "")})'
                else:
                    hint = cr['courseName']
                logger.info(f'[课程信息] | {hint}')

                books = await spider.get_books(cr["courseId"]) or []
                if detail and not books:
                    logger.warning(f'[书籍信息] | （该课程下没有教材任务点）')
                for book in books:
                    if detail:
                        hint = f'[{cr["courseId"]}-{book["bookId"]}] {book["bookName"]}({book.get("percent", 0)}%)'
                    else:
                        hint = f'{book["bookName"]}'
                    logger.info(f'[书籍信息] | {hint}')
    except Exception:
        logger.warning(f'[读取信息] | 读取信息出错：\n{traceback.format_exc()}')


def _score_of(q_type, q_score, q_no):
    """按题型抽一个合理的得分比例，返回该题得分。"""
    ranges = _paper_config['random-score']
    if 0 < q_type <= len(ranges) and ranges[q_type - 1] is not None:
        r = ranges[q_type - 1]
        if not isinstance(r, list):
            r = [r, r]
        return q_score * uniform(*r)

    logger.warning(f'[任务点信息] | 题[{q_no}] 未知题型 {q_type}')

    defaults = _paper_config.get('defaults', 'pause')
    if defaults == 'pause':
        return q_score * float(input('[任务点信息] | 请手动输入该题得分 [0-1]:'))

    r = _paper_config.get('default-score', [0.8, 1])
    if not isinstance(r, list):
        r = [r, r]
    return q_score * uniform(*r)


async def _analyze(spider, paper_id):
    """解析任务点，返回 (得分, 题数, 内容页数)。

    页数用 XML 里 element 节点数近似——它和教材实际翻页量正相关，
    用来给"学习时长"定标尺，比单纯数题目更贴近真实阅读耗时。
    """
    paper = BeautifulSoup((await spider.get_paper(paper_id))['paperData'], 'lxml-xml')
    questions = paper.select('element[knowledge]:has(> question_type)')
    pages = len(paper.select('element'))

    if not questions:
        return 100, 0, pages

    total = 0.0
    score = 0.0
    for q in questions:
        try:
            q_type = int(q.select_one('question_type').text)
            q_score = float(q.select_one('question_score').text)
        except (AttributeError, TypeError, ValueError):
            logger.warning(f'[任务点信息] | 题目结构异常，已跳过：{str(q)[:80]}')
            continue
        total += q_score
        if q_score > 0:
            q_no = q.select_one('question_no')
            score += _score_of(q_type, q_score, q_no.text if q_no else '?')

    if total <= 0:
        return 100, len(questions), pages

    return int(100 * score / total), len(questions), pages


async def _flash(course_id, book_id, spider, pacing=None):
    pacing = pacing or Pacing(configs.get('time'))
    try:
        browser = await Browser.connect()
        if browser is None:
            logger.error('[刷任务点] | 无法连接客户端，请先打开 iSmart 客户端')
            return

        page = await browser.wait_for_page(r'https?://.*')

        # With Spider
        await spider.login(**configs['user'])
        user_id = (await spider.user_info())['data']['uid']
        book_type = (await spider.book_info(book_id))['bookType']
        root = await spider.get_tasks(book_id, book_type, course_id)

        done = _load_ledger() if _paper_config['skip-finished'] else {}
        logger.info(f'[记账] | 本地已记录 {len(done)} 个任务点（{_ledger_path()}）')

        async def dfs(node, depth=0):
            if pacing.exhausted:
                return
            task = node.task
            logger.info(f"[刷任务点] | {PLACEHOLDER * depth} {task.get('name', task.get('book_id'))}")

            if _paper_config['skip-finished'] and (
                task.get('unitStudyPercent') == 100 or task.get('id') in done
            ):
                logger.info(f"[刷任务点] | {PLACEHOLDER * depth} 跳过（已完成）")
                return

            if 'paperId' in task:  # 有题目才算任务点
                score, questions, pages = await _analyze(spider, task['paperId'])
                plan = pacing.plan(questions, pages)
                await pacing.wait(plan)  # 先"学"，再提交

                result = await page.submit(
                    book_id, task['chapterId'], task['id'], score, plan.seconds, 100, user_id
                )
                ok = not result.get('wasThrown') and result.get('result', {}).get('value')
                if ok:
                    _mark_done(task['id'], task.get('name'), plan.seconds, score)
                    logger.success(
                        f'[刷任务点] | {PLACEHOLDER * depth} 提交成功 得分 {score} 用时 {plan.seconds}s'
                    )
                else:
                    logger.warning(
                        f'[刷任务点] | {PLACEHOLDER * depth} 任务[{task.get("name")}]可能提交失败：{result}'
                    )
                await pacing.maybe_break()

            for ch in node.child:
                await dfs(ch, depth + 1)

        await dfs(root)
        pacing.stats()
    except Exception:
        logger.warning(f'[刷任务点] | 刷任务点出错：\n{traceback.format_exc()}')


async def flash_by_id(identity, pacing=None):
    course_id, book_id = identity.split('-')
    async with Spider() as spider:
        await _flash(course_id.strip(), book_id.strip(), spider, pacing)


async def flash_current(pacing=None):  # 对当前课程或书籍执行刷课
    browser = await Browser.connect()
    if browser is None:
        logger.error('[当前课程] | 无法连接客户端，请先打开 iSmart 客户端')
        return
    course_id, book_id = await browser.get_current()
    logger.info(f'[当前课程] | courseId={course_id} bookId={book_id}')
    pacing = pacing or Pacing(configs.get('time'))
    async with Spider() as spider:
        if book_id:
            await _flash(course_id, book_id, spider, pacing)
        else:
            await spider.login(**configs['user'])
            books = await spider.get_books(course_id) or []
            for book in books:
                await _flash(course_id, book['bookId'], spider, pacing)


async def flash_all(pacing=None):
    pacing = pacing or Pacing(configs.get('time'))
    async with Spider() as spider:
        await spider.login(**configs['user'])
        courses = await spider.get_courses() or []
        for course in courses:
            course_id = course['courseId']
            books = await spider.get_books(course_id) or []
            for book in books:
                await _flash(course_id, book['bookId'], spider, pacing)
