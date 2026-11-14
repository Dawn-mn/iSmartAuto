import argparse
import asyncio
import sys

from loguru import logger

from automaton import utils
from automaton.pacing import Pacing
from configs import configs


class Help(object):
    args = ('-h', '--help')
    kwargs = {
        'action': 'help',
        'default': argparse.SUPPRESS,
        'help': '显示本帮助并退出'
    }


async def main():
    parser = argparse.ArgumentParser('main.py', add_help=False)
    parser.add_argument(*Help.args, **Help.kwargs)

    parser.add_argument('-v', dest='level', action='count', help='日志过滤等级，依次为 WARNING, INFO, DEBUG')
    subparsers = parser.add_subparsers(dest='method', help='模式选择', required=True)

    mode_list = subparsers.add_parser('list', add_help=False, help='列出所有课程和书籍')
    mode_list.add_argument('-d', '--detail', action='store_true', help='显示详细信息')
    mode_list.add_argument(*Help.args, **Help.kwargs)

    mode_flash = subparsers.add_parser('flash', add_help=False, help='对选定的课程执行刷课')
    target = mode_flash.add_mutually_exclusive_group(required=True)
    target.add_argument('-i', '--id', help='指定书籍 ID（格式 courseId-bookId）')
    target.add_argument('-c', '--current', action='store_true', help='限定为当前课程或书籍')
    target.add_argument('-a', '--all', action='store_true', help='刷全部课程（慎用 除非你知道自己在做什么）')

    mode_flash.add_argument('--limit', type=int, help='本次最多处理多少个任务点（先小步验证用）')
    mode_flash.add_argument('--budget-minutes', type=float, help='本次累计上报时长上限（分钟）')
    mode_flash.add_argument('--fast', action='store_true', help='不真实等待，仅用于验证提交链路')
    mode_flash.add_argument(*Help.args, **Help.kwargs)

    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level=['WARNING', 'INFO', 'DEBUG'][args.level or 0], enqueue=False)

    if args.method == 'list':
        await utils.list_books(detail=args.detail)
        return

    pacing = None
    if getattr(args, 'limit', None) or getattr(args, 'budget_minutes', None) or getattr(args, 'fast', False):
        pacing = Pacing(
            configs.get('time'),
            limit=args.limit,
            budget_minutes=args.budget_minutes,
            force_mode='fast' if args.fast else None,
        )

    if args.id:
        await utils.flash_by_id(args.id, pacing)
    elif args.current:
        await utils.flash_current(pacing)
    elif args.all:
        await utils.flash_all(pacing)


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
