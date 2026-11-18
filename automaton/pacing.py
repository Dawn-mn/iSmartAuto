"""学习时长模型。

平台会记录每个任务点上报的学习秒数。这个数字不是孤立的：它和两次请求的
时间戳放在一起可以互相验证，因此它有三个约束：

1. **体量一致性**：页数多、题目多的单元，秒数应当更大；否则同一门课里
   1 页的单元比 9 页的单元耗时更长，数据自身就矛盾。
2. **墙钟一致性**：上报的累计秒数不能超过两次提交之间真实经过的时间；
   否则把时间戳一减就是负值。这是三个量里唯一能被服务端独立验证的。
3. **间隔形态**：人类作答间隔是右偏的、有长尾，不会精确落在某个区间；
   均匀分布的随机值在统计形态上与之不同，因此这里用对数正态采样。

旧实现用 `randint(40, 90) * 题数`，且算完不等待——20 个任务点可能 1 分钟
全部提交完，却声称学了 10 小时。这里改成"估算 + 对数正态采样 + 真实等待 +
间歇休息"，让上报值与真实耗时保持一致。
"""

import asyncio
import math
import os
import random
import time
from dataclasses import dataclass

from loguru import logger


@dataclass
class TaskPlan:
    seconds: int          # 最终上报的秒数
    estimate: float       # 按内容体量算出的期望值
    questions: int
    pages: int

    def describe(self):
        return (f'{self.seconds}s（估算 {self.estimate:.0f}s，'
                f'{self.pages} 个元素 / {self.questions} 道题）')


def _urange(pair, default):
    """从 [lo, hi] 里取随机值，配置写错时回退到默认值。"""
    try:
        lo, hi = pair
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return float(default)
    return random.uniform(min(lo, hi), max(lo, hi))


class Pacing(object):
    def __init__(self, cfg=None, limit=None, budget_minutes=None, force_mode=None):
        self.cfg = cfg or {}
        self.mode = force_mode or self.cfg.get('mode', 'real')
        self.started = time.monotonic()
        self.reported = 0      # 已经上报给平台的累计秒数
        self.tasks = 0
        self.limit = limit                      # 最多处理几个任务点
        self.budget = budget_minutes * 60 if budget_minutes else None

        # 每个账号的时长画像允许整体偏移：均值走 scale，离散度走 sigma。
        # 两个账号若用完全相同的参数，上报秒数的分布形态会过于接近。
        self.scale = float(os.getenv('ISMART_TIME_SCALE') or self.cfg.get('scale', 1.0))
        self.sigma = float(os.getenv('ISMART_TIME_SIGMA') or self.cfg.get('jitter-sigma', 0.35))

    @property
    def exhausted(self):
        if self.limit is not None and self.tasks >= self.limit:
            return True
        if self.budget is not None and self.reported >= self.budget:
            return True
        return False

    # ---------- 估算与采样 ----------
    def plan(self, questions, pages):
        base = float(self.cfg.get('base-seconds', 20))
        per_page = _urange(self.cfg.get('per-page'), 10)
        per_question = _urange(self.cfg.get('per-question'), 40)
        estimate = base + pages * per_page + questions * per_question

        sigma = self.sigma
        factor = math.exp(random.gauss(0.0, sigma))   # 对数正态：右偏、有长尾

        lo, hi = self.cfg.get('clamp', [0.65, 1.7])
        lo, hi = float(lo), float(hi)
        factor = min(max(factor, min(lo, hi)), max(lo, hi))

        cap = float(self.cfg.get('max-task-minutes', 30)) * 60
        seconds = int(min(max(estimate * factor * self.scale, 5.0), cap))
        return TaskPlan(seconds, estimate, questions, pages)

    # ---------- 真实等待 ----------
    async def wait(self, plan):
        if self.mode == 'fast':
            self.reported += plan.seconds
            logger.debug(f'[节奏] | fast 模式不等待：{plan.describe()}')
            return

        logger.info(f'[节奏] | 模拟学习 {plan.describe()} …')
        left = plan.seconds
        next_tick = max(plan.seconds // 3, 30)      # 每 1/3 进度打一次点
        while left > 0:
            chunk = min(60, left)
            await asyncio.sleep(chunk)
            left -= chunk
            if plan.seconds >= 90 and (plan.seconds - left) >= next_tick:
                logger.info(f'[节奏] | 已学习 {plan.seconds - left}/{plan.seconds} 秒')
                next_tick += max(plan.seconds // 3, 30)

        self.reported += plan.seconds
        self.tasks += 1

        elapsed = time.monotonic() - self.started
        if self.reported > elapsed * 1.05 + 5:
            logger.warning(
                f'[节奏] | 上报时长 {self.reported}s 已超过实际经过 {elapsed:.0f}s，'
                '服务端若做时间戳比对会显得异常'
            )

    async def maybe_break(self):
        """任务点之间偶尔插一段长间隔（喝水、走神、切窗口）。"""
        chance = float(self.cfg.get('break-chance', 0.0))
        if self.mode == 'fast' or chance <= 0 or random.random() >= chance:
            return
        seconds = _urange(self.cfg.get('break-seconds'), 180)
        logger.info(f'[节奏] | 插入休息 {seconds:.0f} 秒（模拟临时离开）')
        await asyncio.sleep(seconds)

    # ---------- 自查 ----------
    def stats(self):
        elapsed = time.monotonic() - self.started
        logger.info(
            f'[节奏] | 本次共 {self.tasks} 个任务点，上报 {self.reported}s，'
            f'实际经过 {elapsed:.0f}s'
        )
        return self.tasks, self.reported, elapsed
