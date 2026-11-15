#!/usr/bin/env python3
"""PRELIMINARY looser re-run of Jev project classification (project-loose-v1).

Targets: rows that project-v1 labelled WEB_DEV_STUDY, OTHER_PROJECT or UNCLEAR.
Differences from project-v1 (user instructions, chat 2026-11-15):
  - the WHOLE conversation (every visible user/assistant message, untruncated) is sent;
    only if the API rejects the size is it shortened, and that is recorded;
  - keyword evidence alone can assign a project, whatever the tech area (not only frontend);
  - in-period conversations get a higher project weight (x1.3);
  - thresholds are lower. Results are a preliminary proposal, NOT a confirmed label.
Reads project-v1 outputs read-only and reuses its functions without changing them.
Writes only to results/jev-project-loose-2026-11-15/.

  python3 tools/jev_project_loose.py preview 1   # request size summary for target 1, no API call
  python3 tools/jev_project_loose.py run [N]     # resumable
  python3 tools/jev_project_loose.py report

Removal: delete this file and results/jev-project-loose-2026-11-15/.
"""
import collections
import importlib.util
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('v1', ROOT / 'tools/jev_project_classify.py')
v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v1)

OUT = ROOT / 'results/jev-project-loose-2026-11-15'
V1_RESULTS = v1.OUT / 'results.jsonl'
TARGET = ('WEB_DEV_STUDY', 'OTHER_PROJECT', 'UNCLEAR')
RULE_VERSION = 'project-loose-v1'
PROJECTS = v1.PROJECTS
IN_PERIOD_BOOST = 1.3
T_JEV_PROJECT, T_JEV_MENTION, T_NONPROJECT, T_WRITING = 0.5, 0.5, 0.5, 0.5
FALLBACK_SHRINK = [None, 4000, 1500]  # per-message char cap used only after a size rejection


def date_weight(project, ym):
    w, phase = v1.date_weight(project, ym)
    return (w * IN_PERIOD_BOOST if phase == 'in_period' else w), phase


def targets():
    old = {json.loads(l)['id']: json.loads(l) for l in V1_RESULTS.read_text().splitlines()}
    items = [i for i in v1.manifest() if old[i['id']]['final_category'] in TARGET]
    return items, old


def full_state(item, messages, cap):
    from tools.chatgpt_pipeline.core import redact
    msgs = [{'role': r, 'text': redact(t if cap is None else t[:cap])} for r, t in messages]
    return {'title': item['title'], 'messages': msgs}


def decide(answers, ev, ym):
    probs = dict(answers['category']['probabilities'])
    weights = {p: date_weight(p, ym) for p in PROJECTS}
    adj = {c: probs.get(c, 0.0) * (weights[c][0] if c in weights else 1.0) for c in v1.CATEGORIES}
    s = sum(adj.values()) or 1.0
    q = {c: v / s for c, v in adj.items()}
    noul = lambda k: float(answers[k]['noul'])
    writing, work = noul('is_writing_about_experience'), noul('is_project_work')
    mentions = {p: noul('mentions_' + p) for p in PROJECTS}
    allowed = [p for p in PROJECTS if weights[p][1] != 'before_start']

    final, basis = None, None
    if writing >= T_WRITING:
        final = 'COVER_LETTER' if q['COVER_LETTER'] >= q['INTERVIEW_PREP'] else 'INTERVIEW_PREP'
        basis = 'writing_about_experience'
    else:
        kw = sorted((p for p in allowed if ev[p]['score'] >= 1), key=lambda p: (ev[p]['score'], q[p]), reverse=True)
        if kw and (len(kw) == 1 or ev[kw[0]]['score'] > ev[kw[1]]['score']):
            final, basis = kw[0], 'keyword'
        elif kw:
            final, basis = None, 'keyword_tie:' + ','.join(kw)
        if final is None:
            jp = max(allowed, key=lambda p: q[p]) if allowed else None
            if jp and q[jp] >= T_JEV_PROJECT and mentions[jp] >= T_JEV_MENTION:
                final, basis = jp, 'jev'
    candidate = None
    if final not in PROJECTS:
        cands = [p for p in allowed if q[p] >= 0.2 or ev[p]['score'] >= 1 or mentions[p] >= 0.4]
        if cands:
            best = max(cands, key=lambda p: (ev[p]['score'], q[p]))
            candidate = {'project': best, 'score': round(q[best], 3), 'mention': mentions[best],
                         'keyword_score': ev[best]['score']}
        if final is None:
            rest = {c: v for c, v in q.items() if c not in PROJECTS and not (candidate and c == 'OTHER_PROJECT')}
            rs = sum(rest.values()) or 1.0
            top = max(rest, key=rest.get)
            final = top if rest[top] / rs >= T_NONPROJECT else 'UNCLEAR'
            basis = (basis + ';' if basis else '') + 'non_project'
    conf = q.get(final, 0.0) if final in q else 0.0
    return {'final_category': final, 'basis': basis, 'confidence': round(conf, 3), 'project_candidate': candidate,
            'adjusted_probabilities': {c: round(v, 4) for c, v in q.items()},
            'date_weights': {p: {'weight': round(w, 3), 'phase': ph} for p, (w, ph) in weights.items()},
            'nouls': {'is_project_work': work, 'is_writing_about_experience': writing,
                      **{'mentions_' + p: v for p, v in mentions.items()}},
            'tech_area': answers['tech_area']['choice']}


def post(body, key):
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    for attempt in range(4):
        req = urllib.request.Request(v1.API_URL, data=data, method='POST', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return 'ok', json.loads(r.read()), attempt
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return 'http_' + str(e.code), e.read().decode('utf-8', 'replace')[:300], attempt
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return 'network', str(e.reason), attempt
    return 'exhausted', '', 3


def run(items, old, upto):
    wall0 = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    key = v1.load_key()
    kw_version, compiled = v1.compile_keywords()
    qs = v1.questions()
    path, fail_path = OUT / 'results.jsonl', OUT / 'failures.jsonl'
    done = {json.loads(l)['id'] for l in path.read_text().splitlines()} if path.exists() else set()
    order = sorted(enumerate(items[:upto], 1), key=lambda t: (t[1]['path'], t[0]))
    req_ms, prep_ms, retries, shrunk, failed = [], [], 0, 0, 0
    for seq, item in order:
        if item['id'] in done:
            continue
        t0 = time.perf_counter()
        msgs = v1.conversation_messages(item)
        ev = v1.keyword_evidence(item['title'] + '\n' + '\n'.join(t for _, t in msgs), compiled)
        t1 = time.perf_counter()
        status, resp, cap, sent, req_total = None, None, None, 0, 0.0
        for cap in FALLBACK_SHRINK:
            state = full_state(item, msgs, cap)
            body = {'model': v1.MODEL, 'state': state, 'questions': qs}
            sent = len(json.dumps(state, ensure_ascii=False))
            ts = time.perf_counter()
            status, resp, attempt = post(body, key)
            req_total += time.perf_counter() - ts
            retries += attempt
            if status == 'ok' or status not in ('http_400', 'http_413', 'http_422'):
                break
        if status != 'ok':
            failed += 1
            with fail_path.open('a') as f:
                f.write(json.dumps({'seq': seq, 'id': item['id'], 'status': status, 'detail': resp}, ensure_ascii=False) + '\n')
            print(f'[fail] #{seq} {status} {str(resp)[:120]}', flush=True)
            continue
        shrunk += cap is not None
        ym = (item['created_at'] or '')[:7] or None
        prev = old[item['id']]
        row = {'seq': seq, 'id': item['id'], 'title': item['title'], 'created_ym': ym,
               'previous_category': prev['final_category'], 'previous_candidate': prev['project_candidate'],
               **decide(resp['answers'], ev, ym),
               'keyword_evidence': {p: e for p, e in ev.items() if e['score'] or e['medium'] or e['high']},
               'jev_category_probabilities': resp['answers']['category']['probabilities'],
               'model': resp.get('model'), 'usage': resp.get('usage'),
               'messages_sent': len(msgs), 'sent_chars': sent, 'per_message_cap': cap,
               'request_ms': round(req_total * 1000, 1), 'prep_ms': round((t1 - t0) * 1000, 1),
               'status': 'preliminary', 'rule_version': RULE_VERSION, 'keywords_version': kw_version}
        with path.open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        req_ms.append(row['request_ms'])
        prep_ms.append(row['prep_ms'])
        print(f"[{len(req_ms)}] #{seq} {prev['final_category']:<13} -> {row['final_category']:<17} "
              f"({row['basis']}) req={row['request_ms']:.0f}ms chars={sent}", flush=True)
    wall = time.perf_counter() - wall0
    timing = {'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
              'wall_seconds_total': round(wall, 1), 'calls_ok': len(req_ms), 'failed': failed,
              'shrunk_after_size_error': shrunk, 'retries': retries,
              'request_seconds_sum': round(sum(req_ms) / 1000, 1), 'prep_seconds_sum': round(sum(prep_ms) / 1000, 1)}
    if req_ms:
        s = sorted(req_ms)
        timing.update(request_ms_avg=round(statistics.mean(s), 1), request_ms_p50=s[len(s) // 2],
                      request_ms_p95=s[max(int(len(s) * 0.95) - 1, 0)], request_ms_max=s[-1])
    runs = json.loads((OUT / 'timing.json').read_text()) if (OUT / 'timing.json').exists() else []
    runs.append(timing)
    (OUT / 'timing.json').write_text(json.dumps(runs, indent=2) + '\n')
    print('timing:', json.dumps(timing))
    report()


def report():
    rows = sorted((json.loads(l) for l in (OUT / 'results.jsonl').read_text().splitlines()), key=lambda r: r['seq'])
    head = 'seq\tid\tcreated_ym\tprevious\tpreliminary\tbasis\tconfidence\tcandidate\tkeyword_hits\ttitle'
    lines = [head]
    for r in rows:
        c = r['project_candidate'] or {}
        hits = '; '.join(f"{p}:{','.join(e['high'][:5])}" for p, e in r['keyword_evidence'].items() if e['high'])
        lines.append('\t'.join(map(str, [r['seq'], r['id'], r['created_ym'], r['previous_category'], r['final_category'],
                                         r['basis'], r['confidence'], c.get('project', ''), hits, r['title']])))
    (OUT / 'preliminary.tsv').write_text('\n'.join(lines) + '\n')
    changed = [lines[0]] + [l for l in lines[1:] if l.split('\t')[3] != l.split('\t')[4]]
    (OUT / 'changes.tsv').write_text('\n'.join(changed) + '\n')
    print('rows:', len(rows), '| preliminary:', dict(collections.Counter(r['final_category'] for r in rows).most_common()))
    print('moved:', dict(collections.Counter((r['previous_category'], r['final_category']) for r in rows
                                             if r['previous_category'] != r['final_category']).most_common()))
    print('basis:', dict(collections.Counter(r['basis'] for r in rows).most_common()))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'report':
        report()
        return
    items, old = targets()
    if cmd == 'run':
        run(items, old, int(sys.argv[2]) if len(sys.argv) > 2 else len(items))
    elif cmd == 'preview':
        item = items[int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 0]
        st = full_state(item, v1.conversation_messages(item), None)
        print(json.dumps({'title': st['title'], 'messages': len(st['messages']),
                          'chars': len(json.dumps(st, ensure_ascii=False)),
                          'previous': old[item['id']]['final_category']}, ensure_ascii=False))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
