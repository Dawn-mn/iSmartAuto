import os

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))

# 用绝对路径读配置，避免"必须在仓库根目录执行"的隐性约束
CONFIG_PATH = os.path.join(_HERE, 'configs.yml')
# 本地私有覆盖，默认被 .gitignore 排除：放账号密码等不该进版本库的东西
LOCAL_PATH = os.path.join(_HERE, 'configs.local.yml')


def _deep_merge(base, extra):
    """递归合并：dict 逐键合并，其余类型直接覆盖。"""
    if not isinstance(base, dict) or not isinstance(extra, dict):
        return extra
    out = dict(base)
    for key, val in extra.items():
        out[key] = _deep_merge(out.get(key), val) if key in out else val
    return out


with open(CONFIG_PATH, 'r', encoding='utf-8') as _fp:
    configs = yaml.safe_load(_fp) or {}

if os.path.exists(LOCAL_PATH):
    with open(LOCAL_PATH, 'r', encoding='utf-8') as _fp:
        configs = _deep_merge(configs, yaml.safe_load(_fp) or {})

# 环境变量最高优先：连本地文件都不用写，跑完不留痕
for _key, _env in (('username', 'ISMART_USER'), ('password', 'ISMART_PASSWORD')):
    _val = os.environ.get(_env)
    if _val:
        configs.setdefault('user', {})[_key] = _val


def update(conf):
    configs.update(conf)


if __name__ == '__main__':
    import json
    print(json.dumps(configs, indent=4))
