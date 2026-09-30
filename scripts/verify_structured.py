# -*- coding: utf-8 -*-
"""xhs-note-fetch 加工段：结构化产物（题库 / 清单）的质量闸门。

背景：长图里的内容靠 AI 逐张读图转录，转录完**必须机器校验**，
否则会产生「题号错位」「答案不在选项里」「B 级证据没写依据」「改 JSON 时手误窜答案键」这类静默错误。
本脚本就是把 SKILL.md §加工段 的那些人工纪律变成可执行检查。

用法:
    python3 verify_structured.py <结构化.json> [--schema qa|list] [--baseline 旧版.json]
    python3 verify_structured.py --selftest          # 自检：错误样本必须报 FAIL

schema=qa（题库）每条:
    {"no":1,"stem":"题干","options":{"A":"…","B":"…"},"key":["A"],
     "grade":"A"|"B","basis":"依据来源","verdict":"ok"|"check"|"bad"}
schema=list（清单/步骤）每条:
    {"no":1,"title":"条目","content":"要点","grade":"A"|"B","basis":"…"}

退出码: 0 = 全部通过； 1 = 有 ERROR； 2 = 用法/输入错误
"""
import json
import os
import sys
import tempfile

QA_REQUIRED = ['no', 'stem', 'options', 'key']
LIST_REQUIRED = ['no', 'title', 'content']
GRADES = ('A', 'B')


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def items_of(doc, schema):
    """宽容入参：裸列表，或 {"items":[...]}，或 {"data":[...]}。"""
    if isinstance(doc, list):
        return doc
    for k in ('items', 'data', 'questions', 'list'):
        if isinstance(doc.get(k), list):
            return doc[k]
    return None


def check(items, schema):
    errors, warns = [], []
    if not isinstance(items, list) or not items:
        return ['产物为空或不是列表'], warns

    seen_no = {}
    for i, it in enumerate(items, 1):
        tag = it.get('no', '?') if isinstance(it, dict) else '?'
        if not isinstance(it, dict):
            errors.append('第 %d 条不是对象' % i)
            continue

        req = QA_REQUIRED if schema == 'qa' else LIST_REQUIRED
        for k in req:
            if k not in it or it[k] in (None, '', {}, []):
                errors.append('#%s 缺必填字段 `%s`' % (tag, k))

        if tag in seen_no:
            errors.append('#%s 题号重复（与第 %d 条冲突）' % (tag, seen_no[tag]))
        else:
            seen_no[tag] = i

        # 证据分级：B 级必须写明依据 —— 不给依据的 B 等于没棱没据的传闻
        grade = it.get('grade')
        if grade is not None:
            if grade not in GRADES:
                errors.append('#%s grade=%r 非法（只能是 A=权威依据 / B=多源一致未拿原文）' % (tag, grade))
            elif grade == 'B' and not (it.get('basis') or '').strip():
                errors.append('#%s grade=B 但 basis 为空（B 级必须写清是哪几条来源一致）' % tag)
            elif grade == 'A' and not (it.get('basis') or '').strip():
                errors.append('#%s grade=A 但 basis 为空（A 级要能指到权威文件/标准/教材）' % tag)
        else:
            warns.append('#%s 未标 grade' % tag)

        if schema == 'qa':
            opts = it.get('options') or {}
            if not isinstance(opts, dict) or not opts:
                errors.append('#%s options 为空或非对象' % tag)
            key = it.get('key')
            if isinstance(key, str):
                key = list(key)
            if not isinstance(key, list) or not key:
                errors.append('#%s key 必须是非空列表' % tag)
            else:
                bad = [k for k in key if k not in opts]
                if bad:
                    errors.append('#%s key=%s 不在选项 %s 内（转录错位/手误）'
                                  % (tag, ''.join(map(str, bad)), sorted(opts)))
            v = it.get('verdict')
            if v is not None and v not in ('ok', 'check', 'bad'):
                errors.append('#%s verdict=%r 非法（只能是 ok / check / bad）' % (tag, v))
            if v == 'check':
                warns.append('#%s 仍是 check（暂存态，应走多源核验三步收口）' % tag)

    # 题号连续性（不要求从 1 开始，但中间不能断）
    nos = sorted(seen_no.keys())
    try:
        ints = [int(n) for n in nos]
        if len(ints) > 1 and ints[-1] - ints[0] + 1 != len(ints):
            warns.append('题号不连续：%s（若为原图跳号可忽略）' % nos)
    except Exception:
        warns.append('题号含非数字：%s' % nos[:5])
    return errors, warns


def answer_signature(items):
    """答案键快照 —— 改 JSON 前后比对，防止重构时手误窜题。"""
    sig = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        k = it.get('key')
        if isinstance(k, str):
            k = list(k)
        sig.append('%s%s' % (it.get('no', '?'), ''.join(map(str, k or []))))
    return ' '.join(sig)


def diff_baseline(cur, base, schema):
    a, b = answer_signature(cur), answer_signature(base)
    out = []
    if schema == 'qa' and a != b:
        for x, y in zip(a.split(), b.split()):
            if x != y:
                out.append('答案键变化：%s → %s' % (y or '(空)', x or '(空)'))
        if len(a.split()) != len(b.split()):
            out.append('条目数变化：%d → %d' % (len(b.split()), len(a.split())))
    return out


def verify(path, schema, baseline=None):
    doc = load(path)
    items = items_of(doc, schema)
    if items is None:
        print('[x] 读不出条目：顶层既不是列表也没有 items/data/questions/list 键')
        return 2
    errors, warns = check(items, schema)
    if baseline:
        b = load(baseline)
        errors += diff_baseline(items, items_of(b, schema), schema)

    grades = {}
    for it in items:
        if isinstance(it, dict):
            grades[it.get('grade')] = grades.get(it.get('grade'), 0) + 1
    checked = sum(1 for it in items if isinstance(it, dict) and it.get('verdict') == 'ok') if schema == 'qa' else 0

    print('文件   : %s' % path)
    print('schema : %s' % schema)
    print('条目数 : %d' % len(items))
    print('证据分级: %s' % ', '.join('%s=%d' % (k or '未标', v) for k, v in sorted(grades.items(), key=lambda x: str(x[0]))))
    if schema == 'qa':
        print('复核结论: ok=%d / %d' % (checked, len(items)))
    if errors:
        print('\n❌ ERROR (%d):' % len(errors))
        for e in errors:
            print('  - ' + e)
    if warns:
        print('\n⚠️ WARN (%d):' % len(warns))
        for w in warns:
            print('  - ' + w)
    print('\n结论   : %s' % ('FAIL（不许交付，先修）' if errors else 'PASS（可交付）'))
    return 1 if errors else 0


SELFTEST_BAD = [
    {'no': 1, 'stem': '题干一', 'options': {'A': '甲', 'B': '乙'}, 'key': ['C'], 'grade': 'B', 'basis': '', 'verdict': 'ok'},
    {'no': 2, 'stem': '题干二', 'options': {'A': '甲'}, 'key': ['A'], 'grade': 'X', 'basis': '某文件', 'verdict': 'ok'},
    {'no': 2, 'stem': '题干二重复', 'options': {'A': '甲'}, 'key': [], 'grade': 'A', 'basis': '', 'verdict': 'ok'},
    {'no': 4, 'stem': '题干四缺 key', 'options': {'A': '甲'}, 'grade': 'A', 'basis': '权威文件', 'verdict': 'check'},
    {'no': 5, 'stem': '', 'options': {}, 'key': ['A'], 'grade': 'A', 'basis': '权威文件', 'verdict': 'ok'},
]
SELFTEST_GOOD = [
    {'no': 1, 'stem': '题干一', 'options': {'A': '甲', 'B': '乙'}, 'key': ['A'], 'grade': 'A', 'basis': '国务院文件第X条', 'verdict': 'ok'},
    {'no': 2, 'stem': '题干二', 'options': {'A': '甲', 'B': '乙'}, 'key': ['B'], 'grade': 'B', 'basis': '三个独立答案源一致', 'verdict': 'ok'},
]


def selftest():
    """必须能报 FAIL —— 只能报 PASS 的自测等于没有自测。"""
    d = tempfile.mkdtemp(prefix='xhs_selftest_')
    bad = os.path.join(d, 'bad.json')
    good = os.path.join(d, 'good.json')
    json.dump(SELFTEST_BAD, open(bad, 'w', encoding='utf-8'), ensure_ascii=False)
    json.dump(SELFTEST_GOOD, open(good, 'w', encoding='utf-8'), ensure_ascii=False)

    print('— 用例1：故意坏样本（缺必填 / 重复题号 / key 越界 / grade 非法 / B 级无依据）—')
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc_bad = verify(bad, 'qa')
    out = buf.getvalue()
    print(out)
    print('— 用例2：干净样本（A + B 各一条，依据齐全）—')
    rc_good = verify(good, 'qa')

    ok = rc_bad == 1 and 'ERROR' in out and rc_good == 0
    print('\n— 用例3：答案键漂移（baseline 比对）—')
    drift = os.path.join(d, 'drift.json')
    arr = json.loads(json.dumps(SELFTEST_GOOD))
    arr[1]['key'] = ['A']            # 把第 2 题答案从 B 改成 A
    json.dump(arr, open(drift, 'w', encoding='utf-8'), ensure_ascii=False)
    rc_drift = verify(drift, 'qa', baseline=good)
    ok = ok and rc_drift == 1

    print('\n自测结论: %s（坏样本 rc=%d / 干净样本 rc=%d / 漂移样本 rc=%d）'
          % ('PASS' if ok else 'FAIL —— 脚本本身有问题，别用', rc_bad, rc_good, rc_drift))
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    if '--selftest' in args:
        sys.exit(selftest())
    paths = [a for a in args if not a.startswith('--')]
    if not paths:
        print(__doc__)
        sys.exit(2)
    schema = 'qa'
    baseline = None
    for i, a in enumerate(args):
        if a == '--schema' and i + 1 < len(args):
            schema = args[i + 1]
        if a == '--baseline' and i + 1 < len(args):
            baseline = args[i + 1]
    if schema not in ('qa', 'list'):
        print('[x] --schema 只支持 qa / list')
        sys.exit(2)
    sys.exit(verify(paths[0], schema, baseline))


if __name__ == '__main__':
    main()
