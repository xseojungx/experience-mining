"""CG-INDEX-SYNC-20261115: classification-final artifacts + new isolated search DB.

Inputs (read-only): body-review ledgers/shards, HOLD user ledger, separate
NON_DEV user recheck ledger, CG-C catalog/search DBs (mode=ro; backup copy).
Writes only under results/chatgpt-index-sync-2026-11-15/. No body extraction,
no link re-search, no NON_DEV body access (only ids/counts of existing rows).
"""
import collections
import json
import shutil
import sqlite3

import finalize_chatgpt_body_review as fin
from chatgpt_body_review import PRIOR, ROOT, dump, sha

TASK = 'CG-INDEX-SYNC-20261115'
NEW = ROOT / 'results/chatgpt-index-sync-2026-11-15'
RECHECK = ROOT / 'results/chatgpt-nondev-user-recheck-2026-11-15/user_decisions.jsonl'
HOLD_LEDGER = fin.OUT / 'reviews' / 'user_decisions.jsonl'
EXPECTED_RECHECK_DEV = {
    '46620c39ce3e2fa828592f791b5eaed8d2c4d77021c1e4ae860fe56dd82984e2',
    'cd801d36d24527702bb805f3b32661c01224a49ae00a21f9a3696d8bf678b450',
    '8aba640314eb6b1ef255cc14a8f91b680227d19838707e590297f1ca37b0df22',
}
PROTECTED_DIRS = ['results/chatgpt-body-review-2026-11-15', 'results/chatgpt-c-2026-11-15', 'data/raw',
                  'tools/chatgpt_pipeline']
PROTECTED_FILES = ['results/chatgpt-nondev-user-recheck-2026-11-15/user_decisions.jsonl',
                   'results/chatgpt-nondev-user-recheck-2026-11-15/README.md', 'state/state.sqlite3',
                   'AGENTS.md', 'spec/MASTER_PLAN.md', 'spec/NOTE_TEMPLATES.md', 'tools/chatgpt_body_review.py']


def protected_hashes(root=ROOT):
    paths = [root / f for f in PROTECTED_FILES]
    for d in PROTECTED_DIRS:
        paths += [p for p in (root / d).rglob('*') if p.is_file() and p.name != '.DS_Store' and '__pycache__' not in p.parts]
    return {str(p.relative_to(root)): sha(p) for p in sorted(paths)}


def ro(path):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return db


def title_categories(catalog):
    db = ro(catalog)
    rows = db.execute('SELECT c.id,c.category,c.rule,c.account,c.conversation_id,c.title,c.pointer,f.path,f.sha256 '
                      'FROM classifications c JOIN files f ON f.id=c.file_id ORDER BY c.id').fetchall()
    db.close()
    keys = ['id', 'category', 'rule', 'account', 'conversation_id', 'title', 'pointer', 'path', 'sha256']
    return {r[0]: dict(zip(keys, r)) for r in rows}


def per_classification_stats(search):
    """Row counts per classification id; ids and counts only, no text."""
    db = ro(search)
    out = {cid: {'messages': 0, 'nodes': 0, 'assets': 0, 'segments': 0, 'links': 0}
           for cid, in db.execute('SELECT classification_id FROM conversation_locations')}
    conv = dict(db.execute('SELECT conversation,classification_id FROM conversation_locations').fetchall())
    queries = {
        'messages': 'SELECT conversation,count(*) FROM messages GROUP BY 1',
        'nodes': 'SELECT conversation,count(*) FROM nodes GROUP BY 1',
        'assets': 'SELECT m.conversation,count(*) FROM assets a JOIN messages m ON a.message=m.id GROUP BY 1',
        'segments': 'SELECT m.conversation,count(*) FROM segments s JOIN messages m ON s.message=m.id GROUP BY 1',
        'links': 'SELECT m.conversation,count(*) FROM links k JOIN segments s ON k.segment=s.id JOIN messages m ON s.message=m.id GROUP BY 1',
    }
    for key, q in queries.items():
        for c, n in db.execute(q):
            out[conv[c]][key] = n
    db.close()
    return out


def final_basis(row):
    if row.get('final_basis') == 'user_recheck_confirmation':
        return 'user_recheck_confirmation'
    if row.get('resolution_basis') == 'user_confirmation':
        return 'user_confirmation_hold_resolution'
    return 'agent_body_review' + ('+independent_recheck' if row.get('independent_checks') else '')


def compact(title, body, missing):
    rec = {'classification_id': title['id'], 'account': title['account'], 'conversation_id': title['conversation_id'],
           'title': title['title'], 'raw_location': {'path': title['path'], 'pointer': title['pointer'], 'sha256': title['sha256']},
           'title_category': title['category'], 'title_rule': title['rule']}
    if body is None:
        rec.update(final_decision=title['category'], final_basis='title-v1', body_reviewed=False)
    else:
        rec.update(final_decision=body['body_decision'], final_basis=final_basis(body), body_rule=body['body_rule'],
                   primary_agent_decision=(body.get('primary') or {}).get('decision'),
                   primary_full_read=body['id'] not in missing,
                   independent_check_decisions=[c['decision'] for c in body.get('independent_checks') or []],
                   hold_user_decision=(body.get('user_resolution') or {}).get('decision'),
                   recheck_user_decision=((body.get('recheck_resolution') or body.get('recheck_confirmation')) or {}).get('decision'))
    return rec


def verify_new(new_path, old_path, dev_ids, nondev_ids, old_stats):
    db = sqlite3.connect('file:' + str(new_path) + '?mode=ro', uri=True)
    db.execute("ATTACH 'file:" + str(old_path) + "?mode=ro' AS old")
    db.execute('CREATE TEMP TABLE nd(id TEXT PRIMARY KEY)')
    db.executemany('INSERT INTO nd VALUES(?)', [(i,) for i in nondev_ids])
    db.execute('CREATE TEMP TABLE nd_conv AS SELECT conversation AS id FROM old.conversation_locations WHERE classification_id IN (SELECT id FROM nd)')
    db.execute('CREATE TEMP TABLE nd_msg AS SELECT id FROM old.messages WHERE conversation IN (SELECT id FROM nd_conv)')
    db.execute('CREATE TEMP TABLE nd_seg AS SELECT id FROM old.segments WHERE message IN (SELECT id FROM nd_msg)')
    one = lambda q: db.execute(q).fetchone()[0]
    nondev_left = {
        'conversation_locations': one('SELECT count(*) FROM main.conversation_locations WHERE classification_id IN (SELECT id FROM nd)'),
        'conversations': one('SELECT count(*) FROM main.conversations WHERE id IN (SELECT id FROM nd_conv)'),
        'nodes': one('SELECT count(*) FROM main.nodes WHERE conversation IN (SELECT id FROM nd_conv)'),
        'messages': one('SELECT count(*) FROM main.messages WHERE id IN (SELECT id FROM nd_msg)'),
        'assets': one('SELECT count(*) FROM main.assets WHERE message IN (SELECT id FROM nd_msg)'),
        'segments': one('SELECT count(*) FROM main.segments WHERE id IN (SELECT id FROM nd_seg)'),
        'links': one('SELECT count(*) FROM main.links WHERE segment IN (SELECT id FROM nd_seg)'),
        'search_runs_chatgpt_to_github': one("SELECT count(*) FROM main.search_runs WHERE direction='chatgpt_to_github' AND source_id IN (SELECT id FROM nd_seg)"),
        'relations': one('SELECT count(*) FROM main.relations WHERE left_id IN (SELECT id FROM nd_conv UNION SELECT id FROM nd_msg UNION SELECT id FROM nd_seg) OR right_id IN (SELECT id FROM nd_conv UNION SELECT id FROM nd_msg UNION SELECT id FROM nd_seg)'),
    }
    old_nondev_rows = {
        'conversations': one('SELECT count(*) FROM nd_conv'), 'messages': one('SELECT count(*) FROM nd_msg'),
        'segments': one('SELECT count(*) FROM nd_seg'),
        'links': one('SELECT count(*) FROM old.links WHERE segment IN (SELECT id FROM nd_seg)'),
    }
    new_ids = {r[0] for r in db.execute('SELECT classification_id FROM main.conversation_locations')}
    db.close()
    new_stats = per_classification_stats(new_path)
    dev_mismatch = [i for i in dev_ids if new_stats.get(i) != old_stats.get(i)]
    rw = sqlite3.connect('file:' + str(new_path) + '?mode=ro', uri=True)
    seg_rowids = {r[0] for r in rw.execute('SELECT rowid FROM segments')}
    fts = {t: {r[0] for r in rw.execute('SELECT rowid FROM ' + t)} for t in ['message_fts', 'substring_fts']}
    checks = {
        'integrity_check': rw.execute('PRAGMA integrity_check').fetchone()[0],
        'foreign_key_errors': len(rw.execute('PRAGMA foreign_key_check').fetchall()),
        'freelist_count': rw.execute('PRAGMA freelist_count').fetchone()[0],
        'fts_rowids_equal_segments': {t: v == seg_rowids for t, v in fts.items()},
        'fts_counts': {t: len(v) for t, v in fts.items()}, 'segments': len(seg_rowids),
        'link_verification_status': dict(rw.execute('SELECT verification_status,count(*) FROM links GROUP BY 1').fetchall()),
        'meta': dict(rw.execute('SELECT key,value FROM meta').fetchall()),
    }
    rw.close()
    return {'nondev_rows_remaining': nondev_left, 'old_rows_for_excluded_nondev': old_nondev_rows,
            'new_classification_ids_equal_dev_set': new_ids == set(dev_ids), 'new_classification_ids': len(new_ids),
            'dev_conversations_with_count_mismatch': dev_mismatch, 'checks': checks, 'new_stats': new_stats}


def run():
    NEW.mkdir(exist_ok=True)
    if (NEW / 'search.sqlite3').exists():
        raise SystemExit('new index already exists; do not overwrite')
    need = int((PRIOR / 'search.sqlite3').stat().st_size * 2.5)
    if shutil.disk_usage(NEW).free < need:
        raise SystemExit('insufficient free disk for copy + VACUUM; need %d bytes' % need)
    before = protected_hashes()
    dump(NEW / 'PROTECTED_BEFORE.json', before)
    (NEW / 'merge').mkdir(exist_ok=True)

    # 1. Agent judgments + HOLD user ledger; only user-resolved unread items are allowed.
    merged, rows = fin.merge(require_complete=True, allow_user_resolved_unread=True, progress_dir=NEW / 'merge')
    missing = set(merged['missing_primary_ids'])
    assert merged['counts'] == {'DEV': 632, 'NON_DEV': 465}, merged['counts']
    assert set(merged['user_resolved_unread_ids']) == missing and len(missing) == 9
    assert not merged['pending_nondev_recheck_ids'] and not merged['unresolved_classification_ids']

    # 2. Separate user recheck ledger over final NON_DEV rows.
    recheck = fin.apply_recheck(rows, RECHECK)
    assert set(recheck['changed_ids']) == EXPECTED_RECHECK_DEV and len(recheck['confirmed_unchanged_ids']) == 13
    body_counts = dict(collections.Counter(r['body_decision'] for r in rows))
    assert body_counts == {'DEV': 635, 'NON_DEV': 462}, body_counts
    dump(NEW / 'BODY_DECISIONS_FINAL.json', rows)

    # 3. Combine with title categories.
    titles = title_categories(PRIOR / 'catalog.sqlite3')
    by_body = {r['id']: r for r in rows}
    title_counts = dict(collections.Counter(t['category'] for t in titles.values()))
    assert title_counts == {'DEV': 236, 'AMBIGUOUS': 1097, 'NON_DEV': 12}, title_counts
    assert {i for i, t in titles.items() if t['category'] == 'AMBIGUOUS'} == set(by_body)
    for i, r in by_body.items():
        assert (r['path'], r['pointer'], r['sha256']) == (titles[i]['path'], titles[i]['pointer'], titles[i]['sha256'])
    final = [compact(titles[i], by_body.get(i), missing) for i in sorted(titles)]
    with (NEW / 'CLASSIFICATION_FINAL.jsonl').open('w') as f:
        for rec in final:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    dev_ids = sorted(r['classification_id'] for r in final if r['final_decision'] == 'DEV')
    nondev_ids = sorted(r['classification_id'] for r in final if r['final_decision'] == 'NON_DEV')
    title_nondev = sorted(r['classification_id'] for r in final if r['title_category'] == 'NON_DEV')
    assert len(dev_ids) == 871 and len(nondev_ids) == 474

    # 4. Old per-conversation counts, then filtered copy.
    old_path = PRIOR / 'search.sqlite3'
    old_stats = per_classification_stats(old_path)
    assert not set(title_nondev) & set(old_stats) and set(dev_ids) <= set(old_stats)
    meta = {'index_sync': TASK + ': title DEV 236 + body DEV 635 (agent + HOLD user ledger + NON_DEV user recheck)',
            'recheck_ledger_sha256': sha(RECHECK), 'hold_user_ledger_sha256': sha(HOLD_LEDGER),
            'link_status': 'historical; re-verification required (재검증 필요). Links filtered only; not re-searched.'}
    index = fin.build_index(rows, out_dir=NEW, extra_meta=meta)

    # 5. Independent verification on the new file.
    v = verify_new(NEW / 'search.sqlite3', old_path, dev_ids, nondev_ids, old_stats)
    new_stats = v.pop('new_stats')
    recheck_segments = {i: {'old': old_stats.get(i), 'new': new_stats.get(i),
                            'in_original_index': i in old_stats and old_stats[i]['segments'] > 0} for i in sorted(EXPECTED_RECHECK_DEV)}
    after = protected_hashes()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    ok = (all(n == 0 for n in v['nondev_rows_remaining'].values()) and v['new_classification_ids_equal_dev_set']
          and not v['dev_conversations_with_count_mismatch'] and v['checks']['integrity_check'] == 'ok'
          and v['checks']['foreign_key_errors'] == 0 and all(v['checks']['fts_rowids_equal_segments'].values())
          and v['checks']['freelist_count'] == 0 and not changed)
    result = {
        'task_id': TASK, 'status': 'PASS' if ok else 'FAIL',
        'approval': 'user chat 2026-11-15 via main session: "인덱스를 최신 분류와 맞춰. 백그라운드로 실행해."',
        'classification': {'title': title_counts, 'body_after_hold_ledger': merged['counts'], 'body_final': body_counts,
                           'final': {'DEV': len(dev_ids), 'NON_DEV': len(nondev_ids)},
                           'recheck_changed_to_dev': sorted(recheck['changed_ids']),
                           'recheck_confirmed_nondev': len(recheck['confirmed_unchanged_ids']),
                           'missing_primary_ids_preserved': sorted(missing), 'user_resolved_unread_ids': sorted(merged['user_resolved_unread_ids'])},
        'index': index, 'verification': v, 'recheck_segments_in_original_index': recheck_segments,
        'protected_changed': changed, 'protected_file_count': len(before),
        'not_done': ['link re-search / candidate re-verification', 'project attribution', 'C03/C04 integration',
                     'new body extraction', 'NON_DEV body access'],
        'inputs': {'recheck_ledger': {'path': str(RECHECK.relative_to(ROOT)), 'sha256': sha(RECHECK)},
                   'hold_user_ledger': {'path': str(HOLD_LEDGER.relative_to(ROOT)), 'sha256': sha(HOLD_LEDGER)},
                   'prior_search': {'path': str(old_path.relative_to(ROOT)), 'sha256': before[str(old_path.relative_to(ROOT))]},
                   'prior_catalog': {'path': str((PRIOR / 'catalog.sqlite3').relative_to(ROOT)), 'sha256': sha(PRIOR / 'catalog.sqlite3')}},
    }
    dump(NEW / 'PROTECTED_AFTER.json', after)
    dump(NEW / 'RESULT.json', result)
    manifest = {str(p.relative_to(ROOT)): sha(p) for p in sorted(NEW.rglob('*')) if p.is_file() and p.name != 'OUTPUT_MANIFEST.json'}
    dump(NEW / 'OUTPUT_MANIFEST.json', manifest)
    print(json.dumps({k: result[k] for k in ['status', 'classification', 'protected_changed']}, ensure_ascii=False))
    print(json.dumps(index, ensure_ascii=False))
    if not ok:
        raise SystemExit('verification failed; see RESULT.json')


if __name__ == '__main__':
    run()
