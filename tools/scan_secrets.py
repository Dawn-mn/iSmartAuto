"""推送前的泄密自检（push 之前跑一遍，别把账号带出去）。

用法:
    python tools/scan_secrets.py .                  # 扫当前目录
    python tools/scan_secrets.py . --known          # 额外用 configs.local.yml 里的真实值反查
    python tools/scan_secrets.py . --json           # 机器可读

退出码: 0=干净 / 1=有 HIGH 命中 / 2=参数错误。
只读，不改任何文件。
"""
import json
import os
import re
import sys

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.uvcache',
             '.gh', '.state', '.build', 'dist', 'build', '.idea', '.vscode'}
SKIP_EXT = {'.pyc', '.pyo', '.png', '.jpg', '.jpeg', '.gif', '.zip', '.exe',
            '.dll', '.pak', '.bin', '.ico', '.woff', '.woff2', '.ttf', '.db'}

# 文档/示例里常见的假值，别误报
DUMMY_VALUES = {
    '123456abcdef', '13800000000', '13800138000', '12345678901',
    'password', 'passwd', 'your_password', '<密码>', '<用户名>',
}

RULES = [
    ('HIGH', '学校学号 GJ 前缀', re.compile(r'GJ\d{6,}')),
    ('HIGH', '手机号/登录名', re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')),
    ('HIGH', 'GitHub token', re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}')),
    ('HIGH', '私钥块', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('HIGH', '身份证号', re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')),
    ('HIGH', '疑似明文密码', re.compile(
        r'(?i)\b(?:pass(?:word|wd)?|pwd|secret)\b\s*[:=]\s*["\']?([A-Za-z0-9!@#$%^&*._-]{6,})')),
    ('MID', '本机用户名路径', re.compile(r'[A-Za-z]:\\+Users\\+[^\\\s"\']+')),
    ('MID', '内网地址', re.compile(r'(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}')),
]


def _mask(text):
    """报告本身也不能变成泄漏源：只留头 3 位。"""
    t = text.strip()
    if len(t) <= 3:
        return '*' * len(t)
    return t[:3] + '*' * (len(t) - 3)


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            yield os.path.join(dirpath, name)


def _known_values(root):
    """从 configs.local.yml 读真实账号密码，用于反查它们有没有被带进别的文件。"""
    path = os.path.join(root, 'configs.local.yml')
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        cfg = yaml.safe_load(open(path, encoding='utf-8')) or {}
    except Exception:
        return {}
    out = {}
    for key, val in (cfg.get('user') or {}).items():
        s = str(val or '').strip()
        if len(s) >= 6:
            out[key] = s
    return out


def scan(root, known=True, as_json=False):
    hits = []
    known_map = _known_values(root) if known else {}
    for path in _iter_files(root):
        try:
            if os.path.getsize(path) > 2_000_000:
                continue
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            for level, label, rx in RULES:
                for m in rx.finditer(line):
                    val = (m.group(1) if m.groups() else m.group(0)).strip()
                    if val in DUMMY_VALUES or val.startswith('<'):
                        continue
                    hits.append({'level': level, 'rule': label, 'file': path,
                                 'line': lineno, 'value': _mask(val)})

        # 反查：真实凭据出现在「除 configs.local.yml 之外」的任何文件里都是事故
        for key, secret in known_map.items():
            if secret in text and os.path.basename(path) != 'configs.local.yml':
                hits.append({'level': 'HIGH', 'rule': f'真实 {key} 泄漏',
                             'file': path, 'line': 0, 'value': _mask(secret)})

    order = {'HIGH': 0, 'MID': 1, 'LOW': 2}
    hits.sort(key=lambda h: (order.get(h['level'], 9), h['file'], h['line']))

    if as_json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return hits

    if not hits:
        print('CLEAN: 没发现需要处理的内容')
        return hits
    print(f'发现 {len(hits)} 处需要人工确认：\n')
    for h in hits:
        print(f"[{h['level']:4}] {h['rule']:14} {os.path.relpath(h['file'])}:{h['line']} -> {h['value']}")
    high = sum(1 for h in hits if h['level'] == 'HIGH')
    print(f"\n合计 HIGH={high} MID={sum(1 for h in hits if h['level'] == 'MID')}")
    return hits


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    root = args[0] if args else '.'
    hits = scan(root, known='--known' in sys.argv, as_json='--json' in sys.argv)
    return 1 if any(h['level'] == 'HIGH' for h in hits) else 0


if __name__ == '__main__':
    sys.exit(main())
