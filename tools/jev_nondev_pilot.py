#!/usr/bin/env python3
"""Jev pilot helper: classify NON_DEV conversation excerpts via the TypeSafe API.

Read-only on originals and review ledgers. Writes only to
results/jev-nondev-pilot-2026-11-15/ (manifest with ids/titles, results, .env).
Opens a raw conversation file only for the item being sent: first 5 visible
user/assistant messages, 500 chars each, after the pipeline's secret/email/
phone/home-path masking.

API mode (key read from results/jev-nondev-pilot-2026-11-15/.env; stdlib only):
  python3 tools/jev_nondev_pilot.py run [N]     # classify first N (default 100); resumable
  python3 tools/jev_nondev_pilot.py report      # id -> category table, no API call
  python3 tools/jev_nondev_pilot.py preview 1   # show the exact request body for item 1, no API call

Playground mode (clipboard):
  python3 tools/jev_nondev_pilot.py questions   # copy the Questions JSON
  python3 tools/jev_nondev_pilot.py next        # copy next State JSON (cursor advances)
  python3 tools/jev_nondev_pilot.py item 7      # copy item 7 again (1-based)

Removal: delete this file and results/jev-nondev-pilot-2026-11-15/ (holds .env).
Nothing else in the repo references them.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
OUT = ROOT / 'results/jev-nondev-pilot-2026-11-15'
ENV_FILE = OUT / '.env'  # git-ignored in this folder; contains TYPESAFE_API_KEY=...
LIMIT, MAX_MSG, MAX_CHARS, EXPECTED = 465, 5, 500, 465  # LIMIT 465 = all final NON_DEV
API_URL, MODEL, QID, REVIEW_BELOW = 'https://api.typesafe.ai/v1/systemone', 'jev-latest', 'is_dev', 0.7

QUESTIONS = {
    QID: {
        "type": "choice",
        "instructions": (
            "Is this conversation about software development? Judge only from the title and the first "
            "few message excerpts. 이 대화가 소프트웨어 개발과 관련된 대화인지 고르세요."
        ),
        "criteria": {
            "DEV": {
                "covers": "Programming, code, errors/debugging, web/app/backend/frontend work, databases, git/GitHub, deployment, dev tools, APIs, CS coursework or technical learning, using AI for coding.",
                "not_for": "Everyday life, travel, food, health, humanities, language study, career talk with no technical content.",
            },
            "NON_DEV": {
                "covers": "Clearly unrelated to software: daily life, travel, food, health, relationships, literature, general writing, non-technical study or advice.",
                "not_for": "Anything that mentions code, tools, projects, tech stacks, or technical learning.",
            },
            "AMBIGUOUS": {
                "covers": "The excerpt is too short or too general to tell, or mixes development and non-development topics.",
            },
        },
    }
}


def clip(text):
    subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)


def load_items():
    """First LIMIT final NON_DEV rows in review-ledger order, without writing ledgers."""
    import finalize_chatgpt_body_review as fin
    fin.dump = lambda *a, **k: None  # merge() must not write PROGRESS.json
    _, final = fin.merge(require_complete=False)
    rows = [r for r in final if r['body_decision'] == 'NON_DEV']
    return rows, [{k: r[k] for k in ('id', 'account', 'conversation_id', 'title', 'pointer', 'path', 'sha256')}
                  for r in rows[:LIMIT]]


def setup_folder():
    """Create the pilot folder with an empty .env and a local .gitignore for it."""
    OUT.mkdir(parents=True, exist_ok=True)
    if not (OUT / '.gitignore').exists():
        (OUT / '.gitignore').write_text('.env\n')
    if not ENV_FILE.exists():
        ENV_FILE.write_text('TYPESAFE_API_KEY=\n')
        ENV_FILE.chmod(0o600)


def manifest():
    setup_folder()
    path = OUT / 'manifest.json'
    if not path.exists() or len(json.loads(path.read_text())['items']) != LIMIT:
        rows, items = load_items()
        if len(rows) != EXPECTED:
            sys.exit(f'NON_DEV count {len(rows)} != expected {EXPECTED}; stop and check the review ledgers.')
        path.write_text(json.dumps({'source_nondev_total': len(rows), 'order': 'review-ledger order (shard A,B,C)',
                                    'items': items}, ensure_ascii=False, indent=2) + '\n')
    return json.loads(path.read_text())['items']


def pointer_get(v, p):
    for k in p.split('/')[1:] if p else []:
        k = k.replace('~1', '/').replace('~0', '~')
        v = v[int(k)] if isinstance(v, list) else v[k]
    return v


_FILE = {'path': None, 'data': None}


def excerpt(item):
    from chatgpt_body_review import sha

    from tools.chatgpt_pipeline.core import redact, text_parts
    raw = ROOT / item['path']
    if _FILE['path'] != item['path']:  # parse + hash each raw file once, not once per conversation
        if sha(raw) != item['sha256']:
            sys.exit('raw file hash changed; stop.')
        _FILE.update(path=item['path'], data=json.loads(raw.read_text()))
    conv = pointer_get(_FILE['data'], item['pointer'])
    mapping = conv.get('mapping') or {}
    chain, node, seen = [], conv.get('current_node'), set()
    while node in mapping and node not in seen:  # root -> current_node, i.e. chronological
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get('parent')
    chain.reverse()
    messages = []
    for n in chain:
        m = n.get('message')
        if not isinstance(m, dict) or (m.get('metadata') or {}).get('is_visually_hidden_from_conversation'):
            continue
        role = (m.get('author') or {}).get('role')
        if role not in ('user', 'assistant'):
            continue
        text = ' '.join(' '.join(p for _, p in text_parts(m.get('content'))).split())
        if not text:
            continue
        messages.append({'role': role, 'text': redact(text)[:MAX_CHARS]})
        if len(messages) == MAX_MSG:
            break
    return {'title': item['title'], 'messages': messages}


def copy_item(index, items):
    item = items[index - 1]
    state = excerpt(item)
    clip(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"[{index}/{len(items)}] copied State to clipboard · id={item['id']}")
    print(f"title: {item['title']} · messages: {len(state['messages'])}")


def load_key():
    """Read the key from the .env file (never printed or logged)."""
    key = None
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition('=')
            if name.strip() == 'TYPESAFE_API_KEY':
                key = value.strip().strip('"\'')
    if not key:
        sys.exit(f'no API key: put it after TYPESAFE_API_KEY= in {ENV_FILE}')
    return key


def call_api(state, key):
    import time
    import urllib.error
    import urllib.request
    body = json.dumps({'model': MODEL, 'state': state, 'questions': QUESTIONS}, ensure_ascii=False).encode('utf-8')
    for attempt in range(4):
        req = urllib.request.Request(API_URL, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f'API error HTTP {e.code}: {e.read().decode("utf-8", "replace")[:300]}')
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            sys.exit(f'network error: {e.reason}')


def run_api(items, upto):
    key = load_key()
    path = OUT / 'jev_results.jsonl'
    done = {json.loads(l)['id'] for l in path.read_text().splitlines()} if path.exists() else set()
    import time
    started = time.time()
    local_s = api_s = 0.0
    calls = 0
    order = sorted(enumerate(items[:upto], 1), key=lambda t: (t[1]['path'], t[0]))  # group by raw file
    for index, item in order:
        if item['id'] in done:
            continue
        t0 = time.time()
        state = excerpt(item)
        t1 = time.time()
        ans = call_api(state, key)['answers'][QID]
        local_s += t1 - t0
        api_s += time.time() - t1
        calls += 1
        row = {'index': index, 'id': item['id'], 'jev_choice': ans['choice'], 'confidence': ans['confidence'],
               'probabilities': ans['probabilities'], 'messages_sent': len(state['messages'])}
        with path.open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        print(f"[{index}/{min(upto, len(items))}] {ans['choice']:<9} conf={ans['confidence']:.2f}  {item['id'][:12]}")
    print(f'elapsed {time.time() - started:.1f}s total | api {api_s:.1f}s | local (read+excerpt) {local_s:.1f}s | '
          f'calls {calls}' + (f' | avg api {api_s / calls * 1000:.0f}ms/call' if calls else ''))
    report(items)


def report(items):
    """id -> category table (TSV) and a summary. Needs no API key."""
    import collections
    path = OUT / 'jev_results.jsonl'
    if not path.exists():
        sys.exit('no results yet; run: python3 tools/jev_nondev_pilot.py run')
    titles = {i['id']: i['title'] for i in items}
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    lines = ['index\tid\tjev_choice\tconfidence\tneeds_review\ttitle']
    for r in rows:
        review = r['jev_choice'] != 'NON_DEV' or r['confidence'] < REVIEW_BELOW
        lines.append(f"{r['index']}\t{r['id']}\t{r['jev_choice']}\t{r['confidence']:.2f}\t{'Y' if review else ''}\t{titles.get(r['id'], '')}")
    (OUT / 'jev_classification.tsv').write_text('\n'.join(lines) + '\n')
    print('counts:', dict(collections.Counter(r['jev_choice'] for r in rows)),
          f'| needs_review (not NON_DEV or conf<{REVIEW_BELOW}):', sum(l.split('\t')[4] == 'Y' for l in lines[1:]))
    print('table:', OUT / 'jev_classification.tsv')
    for l in lines[1:]:
        if l.split('\t')[4] == 'Y':
            print(l)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'questions':
        clip(json.dumps(QUESTIONS, ensure_ascii=False, indent=2))
        print('copied Questions JSON to clipboard (paste into the Questions pane)')
        return
    items = manifest()
    if cmd == 'run':
        run_api(items, int(sys.argv[2]) if len(sys.argv) > 2 else len(items))
    elif cmd == 'report':
        report(items)
    elif cmd == 'preview':  # print the exact request body for item N; no API call
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        print(json.dumps({'model': MODEL, 'state': excerpt(items[n - 1]), 'questions': QUESTIONS},
                         ensure_ascii=False, indent=2))
    elif cmd == 'next':
        cursor_path = OUT / 'cursor.json'
        cursor = json.loads(cursor_path.read_text())['next'] if cursor_path.exists() else 1
        if cursor > len(items):
            sys.exit('all items copied.')
        copy_item(cursor, items)
        cursor_path.write_text(json.dumps({'next': cursor + 1}) + '\n')
    elif cmd == 'item':
        copy_item(int(sys.argv[2]), items)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
