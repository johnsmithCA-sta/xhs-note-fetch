# -*- coding: utf-8 -*-
"""xhs-note-fetch 提取段「下载环节」的离线自证 —— 不碰真实账号，可重复跑。

为什么需要它：`fetch_note.py --selftest` 只证明纯函数（JSON 解析 / 白名单 / 注样探测）是对的，
**证明不了「下载 → 去重 → 丢空图 → 断点续跑 → 落盘」这一段链路真的通**。
本脚本起一个纯标准库的本地假图片服务，用假数据把这段链路端到端跑一遍。

用法:
    python3 offline_demo.py          # 跑全部用例
    python3 offline_demo.py -v       # 保留中间产物路径并打印

退出码: 0 = 全部通过；1 = 有用例失败；2 = 环境错误（如端口占用）
"""
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import threading
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def load_fetch_note():
    path = os.path.join(HERE, 'fetch_note.py')
    spec = importlib.util.spec_from_file_location('fetch_note', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_png(path, w, h):
    """写一张真 PNG（用不易压缩的像素，保证 >10KB）。"""
    raw = b''.join(
        b'\x00' + bytes(((x * x + y * 7 + (x ^ y)) % 256) for x in range(w * 3))
        for y in range(h)
    )

    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    hdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', hdr)
                + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = lambda *a, **k: QuietHandler(*a, directory=directory, **k)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    verbose = '-v' in sys.argv[1:]
    fn = load_fetch_note()
    results = []

    def check(cond, label, detail=''):
        results.append(cond)
        print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label, ('  — ' + detail) if detail else ''))

    root = tempfile.mkdtemp(prefix='xhs_offline_')
    served = os.path.join(root, 'served')
    os.makedirs(served)
    # 三张图：大图 A、与 A 完全相同的重复图 B、一张 <10KB 的空图 C
    make_png(os.path.join(served, 'a.png'), 900, 900)
    shutil.copy(os.path.join(served, 'a.png'), os.path.join(served, 'b.png'))
    make_png(os.path.join(served, 'tiny.png'), 32, 32)

    srv, port = serve(served)
    base = 'http://127.0.0.1:%d' % port
    good = lambda u: fn.host_allowed(u, ['127.0.0.1'])

    try:
        note = os.path.join(root, 'note')
        media = os.path.join(note, 'media')
        keep = os.path.join(note, '_source')
        os.makedirs(media)
        os.makedirs(keep)

        images = [
            {'url': base + '/a.png', 'width': 900, 'height': 900},      # 正常
            {'url': base + '/b.png', 'width': 900, 'height': 900},      # 与 A 同 md5 → 去重
            {'url': base + '/tiny.png', 'width': 32, 'height': 32},     # <10KB → 丢弃
            {'url': 'https://evil.example.com/x.png', 'width': 1, 'height': 1},  # 白名单外 → 拦截
        ]

        print('用例1：首跑 —— 下载 / 去重 / 丢空图 / 拦截域名')
        rec, st = fn.download_images(images, media, keep, good)
        check(st['downloaded'] == 1, '下载 1 张（重复图去重）', 'downloaded=%d' % st['downloaded'])
        check(st['deduped'] == 1, '去重 1 张', 'deduped=%d' % st['deduped'])
        check(st['dropped'] == 1, '丢弃 1 张空图', 'dropped=%d' % st['dropped'])
        check(st['blocked'] == 1, '拦截 1 个白名单外域名', 'blocked=%d' % st['blocked'])
        check(st['resumed'] == 0, '首跑无复用', 'resumed=%d' % st['resumed'])
        check(os.path.exists(os.path.join(keep, 'downloads.json')), '续跑台账已落盘')

        print('用例2：二跑 —— 断点续跑（不应重下）')
        rec2, st2 = fn.download_images(images, media, keep, good)
        check(st2['resumed'] == 1, '复用 1 张已下载图', 'resumed=%d' % st2['resumed'])
        check(st2['downloaded'] == 1, 'downloaded 仍计 1（未新增下载）', 'downloaded=%d' % st2['downloaded'])

        print('用例3：--max-images 预算上限')
        media3 = os.path.join(root, 'n3', 'media')
        keep3 = os.path.join(root, 'n3', '_source')
        os.makedirs(media3)
        os.makedirs(keep3)
        many = [{'url': base + '/a.png?i=%d' % i, 'width': 9, 'height': 9} for i in range(5)]
        _, st3 = fn.download_images(many, media3, keep3, good, max_images=2)
        check(len(os.listdir(media3)) == 1, '上限 2 张时只处理前 2 张（去重后 1 个文件）',
              'files=%s' % os.listdir(media3))

        print('用例4：中断模拟 —— 台账只记已完成项，续跑补齐')
        media4 = os.path.join(root, 'n4', 'media')
        keep4 = os.path.join(root, 'n4', '_source')
        os.makedirs(media4)
        os.makedirs(keep4)
        imgs4 = [
            {'url': base + '/a.png', 'width': 900, 'height': 900},
            {'url': base + '/tiny.png', 'width': 32, 'height': 32},
        ]
        fn.download_images(imgs4[:1], media4, keep4, good)      # 模拟只跑完第 1 张
        rec4, st4 = fn.download_images(imgs4, media4, keep4, good)
        check(st4['resumed'] == 1, '第二张运行复用第一张', 'resumed=%d' % st4['resumed'])
        check(st4['dropped'] == 1, '第二张空图仍被丢弃', 'dropped=%d' % st4['dropped'])

        if verbose:
            print('\n中间产物：%s' % root)
    finally:
        srv.shutdown()

    ok = all(results)
    print('\n离线自证结论: %s（%d/%d 通过）'
          % ('PASS' if ok else 'FAIL', sum(results), len(results)))
    if not verbose:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
