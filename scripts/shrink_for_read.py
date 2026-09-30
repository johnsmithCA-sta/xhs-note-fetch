# -*- coding: utf-8 -*-
"""xhs-note-fetch 加工段：读图前降采样 + token 预算估算。

背景：加工段要用视觉模型「逐张读图」转录，这是整条流水线最贵的一笔多模态开销。
长图笔记动辄十几张 1080x2400 的大图，**原图直读**既慢又贵。本脚本在 Read 之前
先把图降到够看清文字的程度，并打印 token 预算，把「逐张读图」从裸奔变成有数。

用法:
    python3 shrink_for_read.py <产物目录|media目录> [--max-edge 1568] [--quality 82]
                              [--budget 40000] [--out _read] [--selftest]

产出:
    <目录>/_read/01.jpg …  降采样副本（仅当确有收益时生成；否则跳过并提示用原图）

退出码:
    0  正常（含「无需降采样」）
    1  自检失败
    2  用法/输入错误
    3  超出 --budget（预算告警，图已生成，但不建议整批直读）

依赖: macOS 自带 `sips`（零第三方包）。
"""
import os
import re
import subprocess
import sys

SIPS = 'sips'
IMG_EXT = ('.webp', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff')


def run(cmd, timeout=60):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or ''), (p.stderr or '')


def dims(path):
    """用 sips 读像素尺寸，返回 (w, h)；失败返回 (0, 0)。"""
    rc, out, _ = run([SIPS, '-g', 'pixelWidth', '-g', 'pixelHeight', path])
    if rc != 0:
        return 0, 0
    w = h = 0
    for line in out.splitlines():
        m = re.search(r'pixelWidth:\s*(\d+)', line)
        if m:
            w = int(m.group(1))
        m = re.search(r'pixelHeight:\s*(\d+)', line)
        if m:
            h = int(m.group(1))
    return w, h


def est_tokens(w, h):
    """视觉 token 粗估：主流模型约 每 750 像素 1 token（量级估算，非精确值）。"""
    return int((w * h) / 750) if w and h else 0


def list_images(d):
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith(IMG_EXT))


def selftest():
    """自检：token 估算与目录扫描的分支判断（不依赖真有图片文件）。"""
    ok = True

    def expect(cond, label):
        nonlocal ok
        print('  [%s] %s' % ('PASS' if cond else 'FAIL', label))
        if not cond:
            ok = False

    print('用例：token 估算')
    expect(est_tokens(0, 0) == 0, '零尺寸返回 0')
    expect(est_tokens(750, 1) == 1, '750 像素=1 token')
    big = est_tokens(1080, 2400)
    small = est_tokens(720, 1600)
    expect(big > small > 0, '大图估算高于降采样后（%d > %d）' % (big, small))

    print('用例：目录扫描容错')
    expect(list_images('/no/such/dir/xyz') == [], '目录不存在返回空列表，不抛异常')

    print('\n自测结论: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def opt_str(argv, name, default=None):
    """支持 `--opt value` 与 `--opt=value` 两种写法。"""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + '='):
            return a.split('=', 1)[1]
    return default


def main():
    argv = sys.argv[1:]
    if '--selftest' in argv:
        sys.exit(selftest())
    args = [a for a in argv if not a.startswith('--')]
    if not args:
        print(__doc__)
        sys.exit(2)
    raw = os.path.abspath(args[0])
    try:
        max_edge = int(opt_str(argv, '--max-edge', '1568'))
        quality = int(opt_str(argv, '--quality', '82'))
        budget = int(opt_str(argv, '--budget', '40000'))
    except ValueError as e:
        print('[x] 数值参数解析失败：%s' % e)
        sys.exit(2)
    outname = opt_str(argv, '--out', '_read')

    media = raw if os.path.basename(raw.rstrip('/')) == 'media' else os.path.join(raw, 'media')
    if not os.path.isdir(media):
        print('[x] 找不到图片目录：%s' % media)
        sys.exit(2)
    files = list_images(media)
    if not files:
        print('[x] %s 下没有可读图片' % media)
        sys.exit(2)

    outdir = os.path.join(os.path.dirname(media) if os.path.basename(media) == 'media' else media, outname)
    os.makedirs(outdir, exist_ok=True)

    print('读图前预处理：%d 张（max-edge=%d，quality=%d）' % (len(files), max_edge, quality))
    total_raw = total_new = 0
    shrunk = 0
    for f in files:
        src = os.path.join(media, f)
        w, h = dims(src)
        t_raw = est_tokens(w, h)
        total_raw += t_raw
        long_edge = max(w, h)
        dst = os.path.join(outdir, os.path.splitext(f)[0] + '.jpg')
        if long_edge > max_edge:
            rc, _, err = run([SIPS, '-s', 'format', 'jpeg', '-s', 'formatOptions', str(quality),
                              '--resampleHeightWidthMax', str(max_edge), src, '--out', dst])
            if rc != 0:
                print('  [!] %s 降采样失败：%s' % (f, (err or '').strip()[:80]))
                total_new += t_raw
                continue
            nw, nh = dims(dst)
            t_new = est_tokens(nw, nh)
            shrunk += 1
            print('  - %s：%dx%d → %dx%d（约 %d → %d token）' % (f, w, h, nw, nh, t_raw, t_new))
        else:
            rc, _, _ = run([SIPS, '-s', 'format', 'jpeg', '-s', 'formatOptions', str(quality),
                            src, '--out', dst])
            t_new = est_tokens(long_edge, min(w, h)) if rc == 0 else t_raw
            print('  - %s：%dx%d（已足够小，仅转 jpg，约 %d token）' % (f, w, h, t_new))
        total_new += t_new

    print('\n降采样 %d 张 → %s' % (shrunk, outdir))
    print('读图预算：原图约 %d token → 降采样后约 %d token（省约 %d%%）'
          % (total_raw, total_new, (100 * (total_raw - total_new) // total_raw) if total_raw else 0))
    if total_raw:
        print('提示：这是**视觉输入**的粗估；实际计费含模型侧开销，量级参考用。')
    if budget and total_new > budget:
        print('[!] 超出预算 %d token（实估约 %d）——建议分批读或先只读关键页' % (budget, total_new))
        sys.exit(3)
    print('建议：Read 时优先读 %s/ 下的降采样副本，不要直读 media/ 原图。' % outname)
    sys.exit(0)


if __name__ == '__main__':
    main()
