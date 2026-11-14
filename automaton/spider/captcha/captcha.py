"""验证码识别（纯 Pillow 实现，不再依赖 OpenCV）

原实现依赖 cv2.imdecode / cv2.threshold / cv2.imread，在 Python 3.12+ 上
需要编译好的 wheel 且体积巨大。这里改用 Pillow 完成同样的四步：

    解码 -> 灰度 -> 二值化(阈值 200) -> 4 段切割 -> 与 0-9 模板做行均值余弦相似度

与原版保持一致的数学口径，额外做两件事：
    1. 返回置信度（top1 - top2 的分差），低置信度时调用方可重取验证码；
    2. 模板只加载一次并缓存。
"""

import os
import traceback
from io import BytesIO

from loguru import logger
from PIL import Image

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')

THRESHOLD = 200                      # 与原实现一致的二值化阈值
Y0, Y1 = 4, 24                       # 纵向裁剪区间
SEGMENTS = [(9 + i * 15, 24 + i * 15) for i in range(4)]   # 4 个字符的横向区间

_models = {}


def _profile(img: Image.Image):
    """把一张 15x20 的二值图压缩成 20 维行均值向量（原算法的核心特征）。"""
    px = img.tobytes()          # 'L' 模式下每个字节就是一个像素
    w = img.size[0]
    return [sum(px[r * w:(r + 1) * w]) / w for r in range(img.size[1])]


def _binarize(img: Image.Image) -> Image.Image:
    """灰度 -> 二值：深于阈值记 255（墨迹），否则记 0（背景）。

    模板与截图都是"白底黑字"，统一按此极性二值化后，两者的行均值向量
    才具有可比性；极性搞反会让余弦相似度整体反向。
    """
    g = img.convert('L')
    return g.point(lambda v: 255 if v <= THRESHOLD else 0)


def _load_models():
    """加载 0-9 模板并缓存。"""
    if _models:
        return _models
    for i in range(10):
        path = os.path.join(MODELS_DIR, f'{i}.png')
        with Image.open(path) as im:
            _models[i] = _profile(_binarize(im.convert('L')))
    return _models


def _cos(a, b):
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def recognize_ex(img_content: bytes):
    """返回 (识别码, 置信度)。置信度 = 每位 top1 与 top2 的最小分差。"""
    models = _load_models()
    with Image.open(BytesIO(img_content)) as raw:
        img = _binarize(raw.convert('L'))

    code, margins = '', []
    for x0, x1 in SEGMENTS:
        seg = img.crop((x0, Y0, x1, Y1))
        vec = _profile(seg)
        ranked = sorted(
            ((str(j), _cos(vec, tpl)) for j, tpl in models.items()),
            key=lambda kv: kv[1], reverse=True
        )
        code += ranked[0][0]
        margins.append(ranked[0][1] - ranked[1][1])

    confidence = min(margins)
    logger.info(f'[验证码识别] | 结果 {code}（置信度 {confidence:.3f}）')
    return code, confidence


def recognize(img_content: bytes):
    """兼容旧接口：只返回识别结果字符串。"""
    try:
        return recognize_ex(img_content)[0]
    except Exception:
        logger.warning(f'[验证码识别] | 识别出错：\n{traceback.format_exc()}')
        return ''
