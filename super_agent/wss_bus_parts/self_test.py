"""The WSS daemon's offline self-test, split out of ``wss_bus`` (#138).

Why a separate module: ``self_test`` is 798 lines of assertions - the single
largest function in the package - inside a file already far past the 300-line
line. ``worker.md`` §3.1.1 asks that a change to an over-long file leave it
smaller, and one self-contained function is the honest unit to move.

**Every module-level name is reached as ``_wb.NAME``, and that is load-bearing.**
The original patched names through ``globals()[name] = ...`` - ``query_task`` to
fake the scheduled task, ``PATHS`` to a throw-away root, ``guardian_thresholds``
to a blocked heartbeat - then restored them. Those names belong to ``wss_bus``
and the code under test reads them there, so the patch sites were rewritten to
``_wb.NAME = ...``: a copy of the namespace would let a patch land on the copy
and the assertion would pass against a module it never touched. ``Path(__file__)``
became ``Path(_wb.__file__)`` for the same reason - the source assertions read
``wss_bus.py``, and pointing them at this file would silently test the wrong text.

The two ``--self-test`` failures present before this move are unrelated to it and
still fail: they assert the autostart ``arguments`` contain an absolute script
path, which does not hold in this checkout.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .. import wss_bus as _wb


def self_test() -> int:
    """Offline assertions: no network, no running daemon. Returns 0 when all pass."""
    fails: list[str] = []
    total = 0

    def check(name: str, cond: bool, detail: str='') -> None:
        nonlocal total
        total += 1
        if not cond:
            fails.append(f"{name}{(' — ' + detail if detail else '')}")
    src = Path(_wb.__file__).read_text(encoding='utf-8')
    legacy = 'kimi' + '_' + 'api'
    body = '\n'.join((ln for ln in src.splitlines() if not ln.strip().startswith('#')))
    check('daemon 不得引用兼容薄壳模块', f'import {legacy}' not in body and f'{legacy}.' not in body)
    f = _wb._flat({'type': 'usage', 'session_id': 's1', 'context_tokens': 7})
    check('帧摊平', f.get('context_tokens') == 7 and f.get('session_id') == 's1')
    _wb.STATE.sessions.clear()
    _wb.absorb({'type': 'task.list', 'session_id': 's1', 'tasks': [{'task_id': 'a', 'status': 'running'}]})
    _wb.absorb({'type': 'task.list', 'session_id': 's1', 'tasks': [{'task_id': 'b', 'status': 'running'}]})
    check('task.list 全量替换', set(_wb.STATE.sessions['s1']['tasks']) == {'b'})
    _wb.absorb({'type': 'task', 'session_id': 's1', 'task_id': 'b', 'status': 'ended', 'outcome': 'completed'})
    check('task ended 保留', _wb.STATE.sessions['s1']['tasks']['b']['status'] == 'ended')
    _wb.absorb({'type': 'interaction', 'session_id': 's1', 'interaction_id': 'i1', 'status': 'pending'})
    check('interaction 增', 'i1' in _wb.STATE.sessions['s1']['pending'])
    _wb.absorb({'type': 'interaction', 'session_id': 's1', 'interaction_id': 'i1', 'status': 'ended', 'outcome': 'approved'})
    check('interaction 删', 'i1' not in _wb.STATE.sessions['s1']['pending'])
    _wb.STATE.subscribed = {'s1': {'ok': True, 'ack_id': 'x'}, 's2': {'ok': True, 'ack_id': 'y'}}
    _wb.STATE.subscribed = {}
    check('subscribed 重连重置', _wb.STATE.subscribed == {})
    _wb.STATE.subscribed = {'s1': {'ok': False, 'ack_id': 'ack1'}}
    _wb._mark_ack({'type': 'ack', 'id': 'ack1', 'code': 50001, 'msg': 'boom', 'payload': {}})
    check('ack 失败不标成功', _wb.STATE.subscribed['s1']['ok'] is False)
    _wb.STATE.subscribed = {'s1': {'ok': False, 'ack_id': 'ack2'}}
    _wb._mark_ack({'type': 'ack', 'id': 'ack2', 'code': 0, 'payload': {'accepted': ['s1'], 'not_found': []}})
    check('ack 成功才标成功', _wb.STATE.subscribed['s1']['ok'] is True)
    pages = [([{'id': 'a'}, {'id': 'b'}], 'b'), ([{'id': 'b'}, {'id': 'c'}], 'c'), ([], '')]
    seen: dict = {}
    for items, _nxt in pages:
        for it in items:
            seen[it['id']] = it
    check('分页去重', set(seen) == {'a', 'b', 'c'})
    tmpdir = Path(tempfile.mkdtemp(prefix='wss_selftest_'))
    try:
        sp = tmpdir / 'seq.json'
        s1 = _wb.SeqStore(sp)
        s1.next()
        s1.next()
        s1.save()
        s2 = _wb.SeqStore(sp)
        check('seq 跨重启连续', s2.seq == 2 and s2.next() == 3)
        s2.note_drop(1, 5, 'rotation')
        s2.save()
        s3 = _wb.SeqStore(sp)
        check('淘汰 gap 记录', bool(s3.dropped) and s3.dropped[0]['to'] == 5)
        jp = tmpdir / 'journal.jsonl'
        jp.write_text('{"request_id":"r1","verb":"send"}\nNOT JSON\n{"request_id":"r2","verb":"abort"}\n', encoding='utf-8')
        j = _wb.Journal(jp)
        check('journal 损坏行跳过', j.ok and j.get('r1') and j.get('r2'))
        bad = _wb.Journal(tmpdir / 'nope' / 'x.jsonl')
        check('journal 缺失仍可用', bad.ok is True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    jdir = Path(tempfile.mkdtemp(prefix='wss_j_'))
    try:
        _wb.STATE.journal = _wb.Journal(jdir / 'j1.jsonl')
        _wb.STATE.journal.put({'request_id': 'dup', 'verb': 'send', 'sid': 's', 'prompt_id': 'p1'})
        st, pl = _wb.control({'verb': 'send', 'sid': 's', 'text': 'x', 'request_id': 'dup'})
        check('已受理不重发', st == 200 and pl.get('state') == 'indeterminate', str(pl)[:80])
        check('已受理文案含禁止重发', '禁止重发' in str(pl.get('err')), str(pl.get('err'))[:60])
        old_j = _wb.STATE.journal
        _wb.STATE.journal = _wb.Journal(jdir / 'j2.jsonl')
        _wb.STATE.journal.ok = False
        st2, pl2 = _wb.control({'verb': 'send', 'sid': 's', 'text': 'x', 'request_id': 'new'})
        check('journal 不可读不重试', st2 == 200 and pl2.get('state') == 'indeterminate')
        _wb.STATE.journal = old_j
        check('三态-业务码原样返回', '"rejected"' in src or 'state": "rejected"' in src)
        _wb.STATE.journal.ok = False
        st3, pl3 = _wb.control({'verb': 'unsubscribe', 'sid': 'session_x'})
        check('unsubscribe 不受坏 journal 阻断', st3 == 200 and pl3.get('state') == 'accepted', str(pl3)[:80])
        _wb.STATE.journal.ok = True
    finally:
        shutil.rmtree(jdir, ignore_errors=True)
    check('单实例锁函数存在', callable(_wb.acquire_lock) and callable(_wb.release_lock))
    need = ('send', 'abort', 'steer', 'interaction', 'cancel-task', 'create', 'profile', 'archive', 'restore', 'takeover', 'unsubscribe')
    missing = [v for v in need if f'verb == "{v}"' not in src and (not (v in ('archive', 'restore', 'takeover') and 'verb in ("archive", "restore", "takeover")' in src))]
    check('控制面动词覆盖', not missing, f'缺 {missing}')
    check('create 免 sid', 'verb != "create"' in src)
    _orig_q = _wb.query_task
    try:
        _wb.query_task = lambda: None
        check('自启-任务不存在判失败', bool(_wb.verify_task(_wb.task_spec())))
        _spec = _wb.task_spec()
        _wb.query_task = lambda: {'name': _wb.AUTOSTART_NAME(), 'state': 'Ready', 'execute': str(Path(_spec['execute']).with_name('wrong.exe')), 'arguments': '"C:/wrong/x.py" --daemon', 'workdir': str(Path(_spec['workdir']).parent / 'wrong')}
        _f2 = _wb.verify_task(_spec)
        check('自启-字段不符判失败', len(_f2) >= 3, str(_f2)[:80])
        _wb.query_task = lambda: {'name': _wb.AUTOSTART_NAME(), 'state': 'Ready', 'execute': _spec['execute'], 'arguments': _spec['arguments'], 'workdir': None}
        _f3 = _wb.verify_task(_spec)
        _hard = [x for x in _f3 if x != '__workdir_unverifiable__']
        check('自启-workdir 不可验时降级不误判', '__workdir_unverifiable__' in _f3 and (not _hard), str(_f3)[:80])
        _wb.query_task = lambda: {'name': _wb.AUTOSTART_NAME(), 'state': 'Ready', 'execute': _spec['execute'], 'arguments': _spec['arguments'], 'workdir': _spec['workdir']}
        check('自启-匹配时零失败', _wb.verify_task(_spec) == [])
    finally:
        _wb.query_task = _orig_q
    _codes = {_wb.AUTOSTART_EXIT_OK, _wb.AUTOSTART_EXIT_REGISTER_FAILED, _wb.AUTOSTART_EXIT_VERIFY_FAILED, _wb.AUTOSTART_EXIT_UNINSTALL_FAILED}
    check('自启-退出码约定', len(_codes) == 4 and 0 in _codes, str(sorted(_codes)))
    _spec_w = _wb.task_spec()
    _exe_name = Path(_spec_w['execute']).name.lower()
    check('自启-动作程序非控制台版（或注明降级）', _exe_name == 'pythonw.exe' or bool(_spec_w.get('execute_note')), '%r %r' % (_exe_name, _spec_w.get('execute_note')))
    check('PATHS 非生产根（自检隔离）', Path(_wb.PATHS.root).resolve() != Path(_wb.production_state_root()).resolve(), f'{_wb.PATHS.root} vs {_wb.production_state_root()}')
    doc = '# x\n\n' + _wb._cfg().decide_section + '\n- A\n\n## 其它\n- B\n'
    sec = _wb._section_of(doc, _wb._cfg().decide_section)
    check('段提取只取目标段', sec is not None and '- A' in sec and ('- B' not in sec), repr(sec))
    doc2 = doc.replace('## 其它\n- B\n', '## 其它\n- B2\n')
    check('改其它段 md5 不变', hashlib.md5(_wb._section_of(doc, _wb._cfg().decide_section).encode()).hexdigest() == hashlib.md5(_wb._section_of(doc2, _wb._cfg().decide_section).encode()).hexdigest())
    check('段不存在返回 None', _wb._section_of('# y\n', _wb._cfg().decide_section) is None)
    check('空段不算命中内容', not (_wb._section_of(_wb._cfg().decide_section + '\n\n## 其它\n- B\n', _wb._cfg().decide_section) or '').strip())
    must_hit_5 = ['## 待拍板（历史遗留）', '## 需拍板：流程变更', '## 待拍板：记录归档策略', '## 待拍板（流程类）', '## 待拍板（已完成项之外的）']
    check('五个真待拍板标题必须全 HIT', all((_wb._heading_matches(h, _wb._cfg().decide_section) for h in must_hit_5)), str([h for h in must_hit_5 if not _wb._heading_matches(h, _wb._cfg().decide_section)]))
    real_cases = ['## 3. 待拍板', '## 待拍板', '## 5. 待你拍板', '## 八、待拍板', '### 31.5 未决判定（待拍板）', '## 风险 / 要拍板的', '## 5. 本单发现、需拍板的两件事']
    check('现役真实标题全 HIT', all((_wb._heading_matches(h, _wb._cfg().decide_section) for h in real_cases)), str([h for h in real_cases if not _wb._heading_matches(h, _wb._cfg().decide_section)]))
    check('已裁决回顾不误报', _wb._heading_matches('## 第 15 批（用户拍板，四项）', _wb._cfg().decide_section) is False)
    check('section_pattern 可精确化', not _wb._heading_matches('## 需拍板的事项（历史）', _wb._cfg().decide_section, '^待拍板$') and _wb._heading_matches('## 3. 待拍板', _wb._cfg().decide_section, '^待拍板$'))
    check('排除词死代码不存在', '_DECIDE' + '_EXCLUDE' not in src)
    check('位置判据存在', '_decide_taut' in src)
    check('A3 默认间隔 ≤1.0s', _wb.DEFAULT_POLL_INTERVAL <= 1.0, f'{_wb.DEFAULT_POLL_INTERVAL}')
    ftmp = Path(tempfile.mkdtemp(prefix='wss_watch_'))
    (ftmp / 'asst_handoff').mkdir(parents=True, exist_ok=True)
    (ftmp / 'pm').mkdir(parents=True, exist_ok=True)
    try:
        cfg = _wb.WatchConfig([{'id': 'handoff', 'kind': 'dir_new_file', 'path': 'asst_handoff', 'event': 'event.file.handoff_created'}, {'id': 'pm_decide', 'kind': 'file_section_md5', 'path_glob': 'pm/*.md', 'section': _wb._cfg().decide_section, 'event': 'event.file.pm_decide_changed'}], 0.2)
        (ftmp / 'asst_handoff' / 'steward_20261107-0000.md').write_text('旧档\n', encoding='utf-8')
        (ftmp / 'pm' / 'demo.md').write_text(doc, encoding='utf-8')
        w = _wb.FileWatcher(cfg, ftmp)
        check('首轮不发送', w.poll_once() == [])
        (ftmp / 'asst_handoff' / 'steward_new.md').write_text('新档\n', encoding='utf-8')
        evs = w.poll_once()
        hs = [p for t, p in evs if t == 'event.file.handoff_created']
        check('新交接档出事件', len(hs) == 1 and hs[0]['name'] == 'steward_new.md')
        check('交接档 role 可推断', bool(hs) and hs[0]['role'] == 'steward')
        check('交接档 payload 不含正文', bool(hs) and set(hs[0]) <= {'name', 'path', 'mtime', 'role', 'size', 'repo_full', 'project', 'key'})
        check('非目标段变化不发', w.poll_once() == [])
        (ftmp / 'pm' / 'demo.md').write_text(doc.replace('- A', '- A2'), encoding='utf-8')
        ps = [p for t, p in w.poll_once() if t == 'event.file.pm_decide_changed']
        check('目标段变化必发', len(ps) == 1 and ps[0]['project'] == 'demo' and (ps[0]['changed'] is True))
        check('段事件带 repo_full 字段', 'repo_full' in ps[0])
        cp = ftmp / 'watch.json'
        cp.write_text('{"watchers":[{"id":"a","kind":"dir_new_file","path":"asst_handoff","event":"e.a"}],"poll_interval_s":1}', encoding='utf-8')
        good = _wb.WatchConfig.load(cp)
        check('配置可加载', not good.error and len(good.watchers) == 1)
        cp.write_text('{ NOT JSON', encoding='utf-8')
        badcfg = _wb.WatchConfig.load(cp, previous=good)
        check('坏配置保旧配置', bool(badcfg.error) and badcfg.watchers == good.watchers)
        for junk in ('{"watchers":[]}', '{"watchers":[{"id":"a","kind":"nope"}]}', '{"watchers":[{"id":"a","kind":"dir_new_file","event":"e"}]}', '[]'):
            cp.write_text(junk, encoding='utf-8')
            b2 = _wb.WatchConfig.load(cp, previous=good)
            check(f'非法配置被拒 {junk[:24]}', bool(b2.error) and b2.watchers == good.watchers)
        check('配置缺失用默认', len(_wb.WatchConfig.load(ftmp / 'nope.json', previous=good).watchers) == len(_wb.default_watchers()))
        ev = _wb.STATE.add_event(_wb.FILE_EVENT_SID, 'event.file.test', {'k': 1})
        check('文件事件契约同构', set(ev) == {'seq', 'ts', 'sid', 'type', 'payload', 'boot_id'} and ev['sid'] == 'file')
        check('/events sid=file 可过滤', all((e['sid'] == 'file' for e in _wb.STATE.events if e['type'] == 'event.file.test')))
        (ftmp / 'h2').mkdir(parents=True, exist_ok=True)
        cfgA = _wb.WatchConfig([{'id': 'h', 'kind': 'dir_new_file', 'path': str(ftmp / 'h2'), 'event': 'e.h'}], 0.2)
        (ftmp / 'h2' / 'old.md').write_text('x\n', encoding='utf-8')
        fw = _wb.FileWatcher(cfgA, ftmp)
        check('A2 首轮不发', fw.poll_once() == [])
        base_h = dict(fw.baseline.get('h', {}))
        cfgB = _wb.WatchConfig(list(cfgA.watchers) + [{'id': 'x', 'kind': 'dir_new_file', 'path': str(ftmp / 'h2'), 'event': 'e.x'}], 0.2)
        reset = fw.reset_baseline(cfgB)
        check('A2 差量只重置新增 id', reset == ['x'], str(reset))
        check('A2 未变 id 基线保留', fw.baseline.get('h') == base_h)
        (ftmp / 'h2' / 'brand_new.md').write_text('y\n', encoding='utf-8')
        evs = fw.poll_once()
        check('A2 热加载同时建文件不漏', any((p.get('name') == 'brand_new.md' for _, p in evs)), str(evs)[:120])
        check('A2 只改配置不误报', fw.poll_once() == [])
        fw.reset_baseline(cfgB, diff=False)
        check('A2 全量重置确实清空', fw.baseline == {} and (not fw.seeded))
        etmp = Path(tempfile.mkdtemp(prefix='wss_empty_'))
        try:
            (etmp / 'pm').mkdir(parents=True, exist_ok=True)
            (etmp / 'pm' / 'full.md').write_text(_wb._cfg().decide_section + '\n- A\n', encoding='utf-8')
            (etmp / 'pm' / 'empty.md').write_text(_wb._cfg().decide_section + '\n\n## 其它\n- B\n', encoding='utf-8')
            ecfg = _wb.WatchConfig([{'id': 'pd', 'kind': 'file_section_md5', 'path_glob': str(etmp / 'pm' / '*.md'), 'section': _wb._cfg().decide_section, 'event': 'e.p'}], 0.2)
            efw = _wb.FileWatcher(ecfg, etmp)
            check('空段首轮不留事件', efw.poll_once() == [])
            check('空段不计命中', efw.matched.get('pd') == ['full'], str(efw.matched.get('pd')))
            check('空段进 empty_sections', efw.empty_sections.get('pd') == ['empty'], str(efw.empty_sections.get('pd')))
            (etmp / 'pm' / 'full.md').write_text(_wb._cfg().decide_section + '\n\n', encoding='utf-8')
            efw.poll_once()
            check('全空时命中为 0', efw.matched.get('pd') == [], str(efw.matched.get('pd')))
            check('清空段不发事件', efw.poll_once() == [])
        finally:
            shutil.rmtree(etmp, ignore_errors=True)
    finally:
        shutil.rmtree(ftmp, ignore_errors=True)
    saved = (_wb.STATE.watchers, _wb.STATE.watch_file_watcher)
    try:
        _wb.STATE.watch_file_watcher = None
        _wb.STATE.watchers = {'dirw': {'kind': 'dir_new_file', 'event': 'e.d', 'last_fire_ts': 0.0, 'fires': 0, 'matched_count': 0, 'matched_files': [], '_warn_ts': 123.0}, 'secw': {'kind': 'file_section_md5', 'event': 'e.s', 'last_fire_ts': 0.0, 'fires': 0, 'matched_count': 0, 'matched_files': [], '_warn_ts': 456.0}, 'sec2': {'kind': 'file_section_md5', 'event': 'e.s2', 'last_fire_ts': 0.0, 'fires': 0, 'matched_count': 2, 'matched_files': ['a', 'b']}}
        s = _wb._watch_summary()
        by = {i['id']: i for i in s['items']}
        check('目录型不出 warning', by['dirw']['warning'] == '', repr(by['dirw']['warning']))
        check('目录型报 file_count', by['dirw'].get('file_count') == 0)
        check('段型 0 命中出 warning', bool(by['secw']['warning']))
        check('段型有命中无 warning', by['sec2']['warning'] == '')
        check('zero_match 只看段型', s['zero_match_warning'] is True)
        check('无下划线内部键', all((not any((str(k).startswith('_') for k in i)) for i in s['items'])), str([[k for k in i if str(k).startswith('_')] for i in s['items']]))
        _wb.STATE.watchers['secw']['matched_count'] = 1
        check('段型补命中后 zero_match=False', _wb._watch_summary()['zero_match_warning'] is False)
    finally:
        _wb.STATE.watchers, _wb.STATE.watch_file_watcher = saved
    check('注入端点存在', 'events/inject' in src)
    check('注入事件名契约（event.* 前缀）', 'not etype.startswith("event.")' in src)
    k1 = _wb.issue_key('Owner/Demo', 12)
    check('全限定键格式 owner/repo#issue', k1 == 'owner/demo#12', k1)
    check('全限定键忽略大小写差异', _wb.issue_key('OWNER/demo', '12') == 'owner/demo#12')
    check('缺 owner 不成键', _wb.issue_key('demo', 12) == '' and _wb.issue_key('', 1) == '')
    check('delivery 键带 evidence 后缀', _wb.delivery_key('o/demo', 12, 'abc') == 'o/demo#12#abc' and _wb.delivery_key('o/demo', 12) == 'o/demo#12')
    check('旧短键候选含项目名与全名', _wb.legacy_issue_keys('o/Demo', 12) == ['o/demo#12', 'demo#12'], str(_wb.legacy_issue_keys('o/Demo', 12)))
    db = {'ownerA/demo#12': {'worker': {'name': 'A', 'session': 'session_a'}}, 'ownerB/demo#12': {'worker': {'name': 'B', 'session': 'session_b'}}}
    check('同名不同 owner 不串台（A）', (_wb.lookup_issue_record(db, 'ownerA/demo', 12) or {}).get('worker', {}).get('name') == 'A')
    check('同名不同 owner 不串台（B）', (_wb.lookup_issue_record(db, 'ownerB/demo', 12) or {}).get('worker', {}).get('name') == 'B')
    check('无全限定记录时不按裸仓名猜测', _wb.lookup_issue_record(db, 'ownerC/demo', 12) is None)
    short_db = {'demo#12': {'worker': {'name': 'S', 'session': 'session_s'}}}
    check('旧短键只读兼容（唯一时可用）', (_wb.lookup_issue_record(short_db, 'ownerA/demo', 12) or {}).get('worker', {}).get('name') == 'S')
    mixed_db = dict(short_db)
    mixed_db['ownerB/demo#12'] = {'worker': {'name': 'B', 'session': 'session_b'}}
    check('旧短键在有歧义时拒绝匹配', _wb.lookup_issue_record(mixed_db, 'ownerC/demo', 12) is None)
    payload = _wb.qualify_event_payload({'project': 'demo', 'issue': 12, 'md5': 'x'}, repo='OwnerA/Demo')
    check('事件 payload 保留 project 字段', payload.get('project') == 'demo')
    check('事件 payload 另加 repo_full 字段', payload.get('repo_full') == 'ownera/demo')
    check('事件 payload 带全限定 key', payload.get('key') == 'ownera/demo#12')
    payload2 = _wb.qualify_event_payload({'project': 'demo', 'issue': 12}, repo='OwnerB/Demo')
    check('同号 issue 两仓 payload 键不同', payload['key'] != payload2['key'])
    bind_file = Path(tempfile.mkdtemp(prefix='wss_bind_')) / 'issue_sessions.json'
    try:
        bind_file.write_text(json.dumps(db), encoding='utf-8')
        check('绑定查表命中全限定键（A）', _wb.bind_worker('ownerA/demo', 12, bind_file)[0] == 'A')
        check('绑定查表命中全限定键（B）', _wb.bind_worker('ownerB/demo', 12, bind_file)[0] == 'B')
        check('绑定查表不回退到歧义短键', _wb.bind_worker('ownerC/demo', 12, bind_file)[0] == 'unknown')
    finally:
        shutil.rmtree(bind_file.parent, ignore_errors=True)
    gtmp = Path(tempfile.mkdtemp(prefix='wss_guard_'))
    _saved_paths = _wb.PATHS
    _saved_prod = _wb._cfg().production_state_root
    _saved_gs = _wb.STATE.guardian_state
    try:
        _wb.PATHS = _wb.Paths(gtmp)
        _wb.production_state_root = lambda: gtmp
        _wb.PATHS.mkdir()
        (gtmp / 'sessions.json').write_text('{}', encoding='utf-8')
        _hb_rel = _wb.GUARDIAN_THRESHOLDS['delivery_watch'][0]
        _hb = gtmp / _hb_rel
        check('① 心跳文件缺失判 missing', _wb._guardian_summary()['delivery_watch']['state'] == 'missing', str(_wb._guardian_summary()['delivery_watch'])[:120])
        _hb.parent.mkdir(parents=True, exist_ok=True)
        _hb.write_text(json.dumps({'ts': time.time()}), encoding='utf-8')
        check('② 新鲜心跳判 ok', _wb._guardian_summary()['delivery_watch']['state'] == 'ok')
        _orig_read = Path.read_text

        def _boom(self, *a, **kw):
            if str(self).endswith('delivery.json'):
                raise PermissionError(13, 'concurrent replace')
            return _orig_read(self, *a, **kw)
        Path.read_text = _boom
        try:
            s2 = _wb._guardian_summary()
            check('② PermissionError 判 unknown（不是 missing）', s2['delivery_watch']['state'] == 'unknown', str(s2['delivery_watch'])[:120])
            check('② read_error 落到 /healthz 记录', s2['delivery_watch'].get('read_error') == 'PermissionError', str(s2['delivery_watch'].get('read_error')))
            gs = {}
            due = _wb._guardian_alert_due(s2, gs)
            check('② unknown 不投递', all((d['name'] != 'delivery_watch' or d['state'] != 'unknown' for d in due)), str([(d['name'], d['state']) for d in due]))
            check('② unknown 不占去重名额', 'delivery_watch' not in gs, str(gs))
        finally:
            Path.read_text = _orig_read
        _hb.write_text('{ NOT JSON', encoding='utf-8')
        s3 = _wb._guardian_summary()
        check('③ 坏 JSON 判 missing', s3['delivery_watch']['state'] == 'missing', str(s3['delivery_watch'])[:120])
        check('③ 坏 JSON 不算 read_error', 'read_error' not in s3['delivery_watch'])
        _hb.unlink()
        gs = {'delivery_watch': {'state': 'stale', 'ts': 0.0}}
        due = _wb._guardian_alert_due({'delivery_watch': {'state': 'unknown', 'age_s': None, 'threshold_s': 300, 'heartbeat': _hb_rel, 'writer': ''}}, gs)
        check('④ unknown 期间不投递且清掉旧名额', due == [] and 'delivery_watch' not in gs)
        due2 = _wb._guardian_alert_due({'delivery_watch': {'state': 'missing', 'age_s': None, 'threshold_s': 300, 'heartbeat': _hb_rel, 'writer': ''}}, gs)
        check('④ 恢复后同一轮可再报 missing', len(due2) == 1, str(due2)[:80])
        _wb.STATE.guardian_state = {}
        _fake = {'a': {'state': 'stale', 'age_s': 999, 'threshold_s': 1, 'heartbeat': 'x', 'writer': ''}, 'b': {'state': 'ok', 'age_s': 0, 'threshold_s': 1, 'heartbeat': 'y', 'writer': ''}}
        _d1 = _wb._guardian_alert_due(_fake, _wb.STATE.guardian_state)
        _d2 = _wb._guardian_alert_due(_fake, _wb.STATE.guardian_state)
        _wb.STATE.guardian_state = {'a': {'state': 'stale', 'ts': time.time()}}
        _d3 = _wb._guardian_alert_due({'a': {**_fake['a'], 'state': 'ok'}}, _wb.STATE.guardian_state)
        check('guardian 去重＋恢复重置', len(_d1) == 1 and (not _d2) and (not _d3) and ('a' not in _wb.STATE.guardian_state))
        extra = gtmp / _wb._cfg().guardian_extra_file
        check('⑥ 无外部注册 ⇒ 只有内置', set(_wb.guardian_thresholds()) == set(_wb.GUARDIAN_THRESHOLDS))
        extra.write_text('{ NOT JSON', encoding='utf-8')
        check('⑥ 坏 JSON ⇒ 内置 + 不崩', set(_wb.guardian_thresholds()) == set(_wb.GUARDIAN_THRESHOLDS))
        extra.write_text(json.dumps({'io': {'heartbeat': '', 'threshold_s': 10}}), encoding='utf-8')
        check('⑥ 单条形状不合 ⇒ 跳过该条', 'io' not in _wb.guardian_thresholds() and set(_wb.guardian_thresholds()) == set(_wb.GUARDIAN_THRESHOLDS))
        extra.write_text(json.dumps({'decide_watch': {'heartbeat': 'hb/x.json', 'threshold_s': 1}}), encoding='utf-8')
        _th = _wb.guardian_thresholds()
        check('⑥ 同名冲突 ⇒ 内置优先', _th['decide_watch'] == _wb.GUARDIAN_THRESHOLDS['decide_watch'], str(_th['decide_watch']))
        extra.write_text(json.dumps({'reporter': {'heartbeat': 'hb/rep.json', 'threshold_s': 120}}), encoding='utf-8')
        _th2 = _wb.guardian_thresholds()
        check('⑥ 合法外部注册生效', _th2.get('reporter', ('', 0, ''))[:2] == ('hb/rep.json', 120.0), str(_th2.get('reporter')))
        check('⑥ 内置三项阈值未被改写', all((_wb.GUARDIAN_THRESHOLDS[n][1] == v for n, v in (('delivery_watch', 900.0), ('decide_watch', 900.0), ('delivery_watch', 900.0)))))
        extra.unlink()
        prod = Path(tempfile.mkdtemp(prefix='wss_prod_'))
        iso = Path(tempfile.mkdtemp(prefix='wss_iso_'))
        try:
            prod.mkdir(parents=True, exist_ok=True)
            prod_file = prod / _wb._cfg().guardian_state_file
            prod_file.write_text('{"probe": {"state": "stale", "ts": 1}}', encoding='utf-8')
            os.utime(prod_file, (1700000000, 1700000000))
            _before = prod_file.stat().st_mtime
            _wb.PATHS = _wb.Paths(prod)
            _wb.production_state_root = lambda: prod
            _orig_env = os.environ.get('WSS_BUS_STATE_DIR')
            os.environ['WSS_BUS_STATE_DIR'] = str(iso)
            _wb.STATE.guardian_state = {}
            _hr = _wb.guardian_check(do_notify=False)
            _after = prod_file.stat().st_mtime
            check('⑦ 隔离跑一轮不碰生产去重状态', _before == _after and _hr['channel'] == 'isolated', f"prod_mtime {_before}->{_after} channel={_hr['channel']}")
            check('⑦ 隔离根下写入自己的去重状态', (iso / _wb._cfg().guardian_state_file).is_file())
            check('⑦ state_dir 等于实际隔离根', _wb.guardian_context()['state_dir'] == str(iso.resolve()), _wb.guardian_context()['state_dir'])
        finally:
            if _orig_env is None:
                os.environ.pop('WSS_BUS_STATE_DIR', None)
            else:
                os.environ['WSS_BUS_STATE_DIR'] = _orig_env
            shutil.rmtree(prod, ignore_errors=True)
            shutil.rmtree(iso, ignore_errors=True)
    finally:
        _wb.PATHS = _saved_paths
        _wb.production_state_root = lambda: _saved_prod
        _wb.STATE.guardian_state = _saved_gs
        shutil.rmtree(gtmp, ignore_errors=True)
    _mis = _wb.missing_injections(_wb.WssBusConfig())
    check('未注入时列出缺项', bool(_mis) and 'state_root' in _mis, str(_mis))
    check('未注入时不静默连生产', [n for n in _mis if n in ('workspace_root', 'state_root', 'production_state_root')] != [], str(_mis))
    ktmp = Path(tempfile.mkdtemp(prefix='wss_key_'))
    _saved_paths = _wb.PATHS
    _saved_prod = _wb._cfg().production_state_root
    try:
        _wb.PATHS = _wb.Paths(ktmp)
        _wb.production_state_root = lambda: ktmp
        kp = ktmp / _wb._cfg().alarm_state_file
        kp.write_text(json.dumps({'ts': time.time(), 'last_state': 'ok', 'sid': 's1'}), encoding='utf-8')
        k1 = _wb._supervisor_key_summary()
        check('supervisor 键 ok', k1['supervisor_key_state'] == 'ok', str(k1)[:100])
        check('supervisor 键带 age', k1['last_supervisor_alarm_age_s'] is not None)
        kp.write_text(json.dumps({'ts': time.time() - 5000, 'last_state': 'dead', 'sid': 's2'}), encoding='utf-8')
        k2 = _wb._supervisor_key_summary()
        check('陈旧状态判 stale', k2['supervisor_key_state'] == 'stale', str(k2)[:100])
        check('陈旧仍给真实 age', k2['last_supervisor_alarm_age_s'] is not None)
        check('陈旧保留 raw 状态', k2.get('supervisor_key_state_raw') == 'dead')
        kp.unlink()
        _wb.STATE.events.clear()
        check('无状态无事件 ⇒ unknown', _wb._supervisor_key_summary()['supervisor_key_state'] == 'unknown')
        _saved_cfg = wb_cfg = _wb.DEFAULT_CONFIG
        _wb.DEFAULT_CONFIG = replace(wb_cfg, alarm_event='event.session.key_dead')
        _wb.STATE.events.append({'seq': 1, 'ts': time.time(), 'sid': 'file', 'type': 'event.session.key_dead', 'payload': {'old_sid': 's9'}, 'boot_id': 'b'})
        k4 = _wb._supervisor_key_summary()
        check('事件环兜底判 dead', k4['supervisor_key_state'] == 'dead', str(k4)[:100])
        check('兜底取到 sid', k4['supervisor_key_sid'] == 's9')
        _wb.STATE.events.clear()
        _wb.DEFAULT_CONFIG = _saved_cfg
    finally:
        _wb.PATHS = _saved_paths
        _wb.production_state_root = lambda: _saved_prod
        shutil.rmtree(ktmp, ignore_errors=True)
    check('healthz 有 supervisor 键三字段', 'supervisor_key_state' in src and 'last_supervisor_alarm_age_s' in src)
    check('healthz 有 guardians 段', '"guardians"' in src)
    check('healthz 有 state_dir 与 port', '"state_dir"' in src and '"port": PORT' in src)
    check('healthz 区分 ok 与 ready', '"ready": ready' in src and '"ok": True' in src)
    _orig_env_base = _wb.kimi_paths._env_base
    _orig_latest = _wb.kimi_paths.latest_instance
    try:
        _wb.kimi_paths._env_base = lambda: None
        _wb.kimi_paths.latest_instance = lambda root=None: {'server_id': 'demo', 'path': 'x', 'port': 58627, 'host': '127.0.0.1', 'mtime': 0.0}
        _line_ok = _wb.announce_upstream()
        check('启动打印含端口', 'port=' in _line_ok and '58627' in _line_ok, _line_ok)
        check('正常情况下标 source=instance', 'source=instance' in _line_ok and 'degraded=False' in _line_ok, _line_ok)
        _wb.kimi_paths.latest_instance = lambda root=None: None
        _line_bad = _wb.announce_upstream()
        check('降级启动明确标示', 'degraded=True' in _line_bad and '降级' in _line_bad, _line_bad)
        check('降级原因写进输出', 'source=default' in _line_bad and '实例注册不可用' in _line_bad, _line_bad)
        _wb.kimi_paths._env_base = lambda: 'http://127.0.0.1:59999'
        _line_env = _wb.announce_upstream()
        check('环境变量来源标 source=env', 'source=env' in _line_env and 'degraded=True' in _line_env, _line_env)
    finally:
        _wb.kimi_paths._env_base = _orig_env_base
        _wb.kimi_paths.latest_instance = _orig_latest
    _penv = os.environ.get('WSS_BUS_STATE_DIR')
    try:
        with_env = Path(tempfile.mkdtemp(prefix='wss_env_'))
        os.environ['WSS_BUS_STATE_DIR'] = str(with_env)
        check('Paths() 认环境变量', _wb.Paths().root == with_env.resolve(), str(_wb.Paths().root))
        explicit = Path(tempfile.mkdtemp(prefix='wss_exp_'))
        check('显式 root 优先', _wb.Paths(explicit).root == explicit.resolve())
        os.environ.pop('WSS_BUS_STATE_DIR', None)
        check('环境变量清空后回落生产根', _wb.Paths().root == Path(_wb.production_state_root()).resolve())
    finally:
        if _penv is None:
            os.environ.pop('WSS_BUS_STATE_DIR', None)
        else:
            os.environ['WSS_BUS_STATE_DIR'] = _penv
        shutil.rmtree(with_env, ignore_errors=True)
        shutil.rmtree(explicit, ignore_errors=True)
    h3 = Path(tempfile.mkdtemp(prefix='wss_h3_'))
    prod3 = Path(tempfile.mkdtemp(prefix='wss_h3p_'))
    _saved_paths = _wb.PATHS
    _saved_prod = _wb._cfg().production_state_root
    try:
        _wb.PATHS = _wb.Paths(h3)
        _wb.production_state_root = lambda: prod3
        ph = prod3 / _wb.GUARDIAN_THRESHOLDS['decide_watch'][0]
        ph.parent.mkdir(parents=True, exist_ok=True)
        ph.write_text('{"ts": 1}', encoding='utf-8')
        os.utime(ph, (1700000000, 1700000000))
        _before = ph.stat().st_mtime
        _wb.write_heartbeat('decide_watch', {'probe': 1})
        check('心跳写当前根', (h3 / _wb.GUARDIAN_THRESHOLDS['decide_watch'][0]).is_file())
        check('心跳不碰生产', ph.stat().st_mtime == _before, f'prod_mtime {_before}->{ph.stat().st_mtime}')
        check('写成功后没有 write_fail 标记', _wb._read_write_fail('decide_watch') is None)
        blocker = h3 / 'blocked'
        blocker.write_text('not a directory', encoding='utf-8')
        _orig_thresholds = _wb.guardian_thresholds
        _wb.guardian_thresholds = lambda: {'decide_watch': ('blocked/hb.json', 900.0, 'probe')}
        try:
            _wb.write_heartbeat('decide_watch', {'probe': 2})
            _wf = _wb._read_write_fail('decide_watch')
            check('写失败留下可见标记', isinstance(_wf, dict), str(_wf)[:120])
            check('标记含 attempts/err/pid', bool(_wf) and _wf.get('attempts') == len(_wb.HEARTBEAT_WRITE_BACKOFF) and ('Error' in str(_wf.get('err'))) and (_wf.get('pid') == os.getpid()), str(_wf)[:160])
        finally:
            _wb.guardian_thresholds = _orig_thresholds
        _wb.write_heartbeat('decide_watch', {'probe': 3})
        check('写恢复后标记自动清除', _wb._read_write_fail('decide_watch') is None)
    finally:
        _wb.PATHS = _saved_paths
        _wb.production_state_root = lambda: _saved_prod
        shutil.rmtree(h3, ignore_errors=True)
        shutil.rmtree(prod3, ignore_errors=True)
    check('repo_full 归一化', _wb.repo_full_of('Owner/Demo') == 'owner/demo')
    check('repo_full 无 owner 返回空', _wb.repo_full_of('demo') == '')
    check('project_of_repo 取仓名', _wb.project_of_repo('Owner/Demo') == 'demo')
    check('issue 为空不成键', _wb.issue_key('o/demo', None) == '')
    check('role 不可识别为 unknown', _wb._role_of('weird_name.md') == 'unknown')
    check('空标题不命中', _wb._heading_matches('', _wb._cfg().decide_section) is False)
    check('标题归一化', _wb._norm_heading('## 3.  待  拍板') == '待 拍板', _wb._norm_heading('## 3.  待  拍板'))
    check('段目标归一化', _wb._norm_section_want(_wb._cfg().decide_section) == _wb._norm_heading(_wb._cfg().decide_section), _wb._norm_section_want(_wb._cfg().decide_section))
    _dup = ''
    try:
        _wb.WatchConfig.validate({'watchers': [{'id': 'a', 'kind': 'dir_new_file', 'path': 'x', 'event': 'e'}, {'id': 'a', 'kind': 'dir_new_file', 'path': 'y', 'event': 'e'}]})
    except ValueError as exc:
        _dup = str(exc)
    check('重复 watcher id 被拒', '重复' in _dup, _dup)
    _poll = ''
    try:
        _wb.WatchConfig.validate({'watchers': [{'id': 'a', 'kind': 'dir_new_file', 'path': 'x', 'event': 'e'}], 'poll_interval_s': 999})
    except ValueError as exc:
        _poll = str(exc)
    check('poll 越界被拒', '越界' in _poll, _poll)
    _pat = ''
    try:
        _wb.WatchConfig.validate({'watchers': [{'id': 'a', 'kind': 'file_section_md5', 'path_glob': 'x/*.md', 'section': 's', 'section_pattern': '([', 'event': 'e'}]})
    except ValueError as exc:
        _pat = str(exc)
    check('非法 section_pattern 被拒', 'section_pattern' in _pat, _pat)
    blocker = Path(tempfile.mkdtemp(prefix='wss_jblk_')) / 'file'
    blocker.write_text('x', encoding='utf-8')
    try:
        jbad = _wb.Journal(blocker / 'sub' / 'j.jsonl')
        jbad.put({'request_id': 'r', 'verb': 'send'})
        check('journal 写失败即判不可信', jbad.ok is False)
    finally:
        shutil.rmtree(blocker.parent, ignore_errors=True)
    _saved_reg = _wb.__dict__.get('REGISTRY_IDS')
    try:
        _wb.REGISTRY_IDS = {'session_reg'}
        _wb.STATE.probe_sids = set()
        check('探针拒绝在册会话', _wb._probe_allowed('session_reg') is False)
        _wb.STATE.probe_sids = {'session_new'}
        check('探针接受自己建的靶子', _wb._probe_allowed('session_new') is True)
    finally:
        _wb.REGISTRY_IDS = _saved_reg
        _wb.STATE.probe_sids = set()
    try:
        _wb.configure(bind_lookup=lambda repo, issue: ('injected', 'session_i'))
        check('注入绑定查表生效', _wb.bind_worker('o/demo', 1)[0] == 'injected')
    finally:
        _wb.configure(bind_lookup=None)
    _p = _wb.qualify_event_payload({'project': 'demo', 'issue': 12}, repo='OwnerA/Demo')
    _p2 = _wb.qualify_event_payload(_p, repo='OwnerA/Demo')
    check('payload 二次限定不漂移', _p2['key'] == _p['key'] == 'ownera/demo#12')
    _p3 = _wb.qualify_event_payload({'project': 'demo'}, repo='OwnerA/Demo')
    check('无 issue 号不造 key', 'key' not in _p3 and _p3['repo_full'] == 'ownera/demo')
    _saved_port = _wb.PORT
    try:
        _wb.configure(port=59999)
        check('configure 注入端口', _wb.PORT == 59999, str(_wb.PORT))
        check('_port_of 用注入端口', _wb._port_of(None) == 59999)
        _wb.configure(port=0)
    finally:
        _wb.configure(port=_saved_port)
    print(f'--self-test: {total - len(fails)}/{total} 通过')
    for f_ in fails:
        print(f'  FAIL  {f_}')
    return 0 if not fails else 1


def isolate(host) -> Path | None:
    """Point ``host`` and the package copy at a throw-away root before ``self_test``.

    Lives beside the assertions because it exists only for them: the self-test
    asserts it never touches production, so both the state root and the
    "production" baseline it compares against are throw-away directories.

    **Two module objects, one configuration** (#138 P0-2). Under ``-m`` the file
    runs as ``__main__`` and the *package* copy is a second object: the assertions
    here reach ``wss_bus`` through ``from .. import wss_bus``, and that import
    builds the package copy. Configuring only ``__main__`` left the assertions
    reading an unconfigured ``_UnconfiguredPaths``, and they raised
    ``RuntimeError: 未注入 state_root`` on the first ``PATHS`` access - the whole
    ``-m`` entry point died before one assertion ran, while the import path stayed
    green. ``hosts`` configures every live identity, and this module's assertions
    then patch the same objects that were isolated.
    """
    return _isolate(host)


def _isolate(host) -> Path | None:
    cfg = host._cfg()
    injected = cfg.self_test_root is not None
    root = Path(cfg.self_test_root or tempfile.mkdtemp(prefix="wss_selftest_run_"))
    root.mkdir(parents=True, exist_ok=True)
    fake_prod = root / "_fake_production"
    fake_prod.mkdir(parents=True, exist_ok=True)
    overrides: dict = {}
    if cfg.workspace_root is None:
        overrides["workspace_root"] = root
    if cfg.production_state_root is None:
        overrides["production_state_root"] = fake_prod
    if cfg.sessions_path is None:
        overrides["sessions_path"] = root / "sessions.json"
    for mod in _identities(host):
        mod.configure(**overrides)
        mod.PATHS = mod.Paths(root)
        mod.PATHS.mkdir()
        mod.STATE.seq = mod.SeqStore(mod.PATHS.seq)
        mod.STATE.journal = mod.Journal(mod.PATHS.journal)
    return None if injected else root


def _identities(host) -> list:
    """Every live ``wss_bus`` module object that must share one throw-away root.

    ``__main__`` under ``-m``, the package copy this module imports, and ``host``;
    de-duplicated because the import path has all three pointing at one module.
    The package copy is imported **here** rather than left to happen later: under
    ``-m`` it does not exist yet when isolation runs, and it must be configured
    before any assertion reaches it.
    """
    import importlib
    import sys

    out = [host]
    pkg = sys.modules.get("super_agent.wss_bus")
    if pkg is None:
        try:
            pkg = importlib.import_module("super_agent.wss_bus")
        except ImportError:            # pragma: no cover  only a broken install
            pkg = None
    if pkg is not None and not any(pkg is m for m in out):
        out.append(pkg)
    return out
