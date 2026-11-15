#!/usr/bin/env python3
"""Classify DEV ChatGPT conversations by project/category with Jev + local signals.

Signals are kept independent and combined here:
  A. Jev (TypeSafe API): category choice, purpose nouls, per-project mention nouls, tech area.
  B. Local keyword evidence over the WHOLE conversation (never sent): keywords.json.
  C. Date weight: conversation month vs project period (down-weights, never removes).
The four projects are assigned tightly; anything doubtful goes to a non-project
category with `project_candidate` kept for review.

Read-only on originals, review ledgers and the search index. Writes only to
results/jev-project-classify-2026-11-15/. Key: results/jev-nondev-pilot-2026-11-15/.env

  python3 tools/jev_project_classify.py preview 1   # exact request body for item 1, no API call
  python3 tools/jev_project_classify.py run [N]     # classify first N (default all); resumable
  python3 tools/jev_project_classify.py report      # tables + summary, no API call

Removal: delete this file and results/jev-project-classify-2026-11-15/.
"""
import collections
import json
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
OUT = ROOT / 'results/jev-project-classify-2026-11-15'
INDEX = ROOT / 'results/chatgpt-index-sync-2026-11-15'
ENV_FILE = ROOT / 'results/jev-nondev-pilot-2026-11-15/.env'
API_URL, MODEL = 'https://api.typesafe.ai/v1/systemone', 'jev-latest'
RULE_VERSION = 'project-v1'
EXPECTED_DEV = 871

# Input size (user: "조금 더 길게"): user messages 600 chars each up to 8,000 total,
# first 3 assistant messages 400 chars each, up to 100 code identifiers.
USER_EACH, USER_TOTAL, ASSIST_N, ASSIST_EACH, IDENT_MAX = 600, 8000, 3, 400, 100

PROJECTS = ['INFLUY', 'ON', 'SPITCHING', 'MANSUMUGANG']
PERIODS = {'INFLUY': ('2025-04', '2025-09'), 'SPITCHING': ('2024-09', '2025-07'),
           'MANSUMUGANG': ('2024-11', '2025-03'), 'ON': ('2024-07', '2025-02')}  # DATA-03, inclusive months
# Thresholds (tune on a gold set later).
T_PROJECT, T_WORK, T_WRITING, T_MENTION, T_NONPROJECT, T_TECH = 0.75, 0.6, 0.5, 0.7, 0.6, 0.6

CATEGORIES = {
    'INFLUY': {
        'covers': 'Building, planning or debugging INFLUY (인플루이): an influencer commerce service where influencers/sellers (셀러) sell to buyers; seller talk box (톡박스) for buyer questions, FAQ cards, question categories, market links, my-market, announcements. CEOS (세오스) club project. React/TypeScript frontend.',
        'not_for': 'General React/TypeScript study; writing a cover letter, resume or interview answer about INFLUY; other commerce projects.',
        'examples': 'talk box bulk reply page bug; FAQ card registration form; seller my-market tabs',
    },
    'ON': {
        'covers': 'Building, planning or debugging ON: a community web app (PWA) for exchange students (교환학생) — accompany/동행 recruiting, chat rooms, country list (국가 목록), abroad life info, Firebase push notifications and service worker.',
        'not_for': 'General PWA/Firebase study; writing about ON for a cover letter or interview; travel planning itself.',
        'examples': 'AccompanyChat header props; FCM background message in service worker; exchange student country list page',
    },
    'SPITCHING': {
        'covers': 'Building, planning, designing or researching SPitching (스피칭): a 2024 graduation capstone (졸업프로젝트/졸프) AI presentation trainer — eye contact (시선 추적), gesture recognition (crossed arms, raised hand, touching face), STT fluency and filler words (추임새), script similarity, Q&A practice chatbot, practice video recording, feedback reports. Includes product planning, UX design and market research on interview/presentation AI services. React frontend, Spring backend, FastAPI/MediaPipe/XGBoost AI server.',
        'not_for': 'General AI/ML coursework not tied to presentation practice; writing about SPitching for a cover letter or interview.',
        'examples': 'GestureBoard component; existing interview AI services research; filler word classifier; PracticePage recording flow',
    },
    'MANSUMUGANG': {
        'covers': 'Building, planning or debugging 만수무강: a service for elderly people (노인, 어르신, 시니어). Node.js/Express backend and AWS infrastructure.',
        'not_for': 'General Node.js or AWS study without the elderly-care service; writing about it for a cover letter or interview.',
        'examples': 'Express API for senior users; deploying the backend to AWS EC2',
    },
    'OTHER_PROJECT': {
        'covers': 'Real project work (team, hackathon, club, side project) that is clearly NOT INFLUY, ON, SPITCHING or 만수무강.',
        'not_for': 'Course homework; general study; any of the four named projects.',
    },
    'COURSE_ASSIGNMENT': {
        'covers': 'A university course coding assignment or lab: implementing a given spec, homework code, grading data, lab reports.',
        'not_for': 'Conceptual CS theory study without an assignment; team product projects.',
        'examples': 'add grades to student data in C; data structures homework',
    },
    'CS_STUDY': {
        'covers': 'Studying computer science theory and courses: computer architecture (컴퓨터구조), information and communication engineering (정보통신공학), artificial intelligence and machine learning (including model practice such as LSTM), C++, WebAssembly (wasm), operating systems, networks, discrete math.',
        'not_for': 'Algorithm/coding-test practice; web framework study; product projects.',
    },
    'ALGORITHM': {
        'covers': 'Algorithm and data-structure problem solving, coding-test practice, complexity analysis.',
        'not_for': 'CS theory courses without problem solving; project code.',
    },
    'WEB_DEV_STUDY': {
        'covers': 'Learning web development (React, JavaScript, CSS, Node, Git, deployment, tooling) that is not directly part of the four projects or another real project.',
        'not_for': 'Work on a specific project; CS theory; algorithms.',
    },
    'COVER_LETTER': {
        'covers': 'Writing or editing a cover letter (자소서) or application essay answers, including describing past projects for an application question.',
        'not_for': 'Actually building the project; interview Q&A practice.',
    },
    'INTERVIEW_PREP': {
        'covers': 'Preparing for interviews (technical or behavioral), portfolio or resume preparation, organizing past experience for interviews.',
        'not_for': 'Cover letter question answers; building the project.',
    },
}
TECH = {'frontend_react': 'React/TypeScript/JS frontend', 'backend_node': 'Node.js/Express backend',
        'backend_spring': 'Java/Spring backend', 'ai_ml': 'AI/ML models and data',
        'cs_theory': 'CS theory (architecture, networks, C++, wasm)', 'algorithm': 'algorithms and data structures',
        'cloud_devops': 'cloud, deployment, infrastructure', 'product_planning_design': 'product planning, UX, design, market research',
        'writing': 'writing about experience (cover letter, resume, interview)', 'other': 'something else'}
TECH_OK = {'INFLUY': {'frontend_react', 'product_planning_design', 'cloud_devops', 'other'},
           'ON': {'frontend_react', 'product_planning_design', 'cloud_devops', 'other'},
           'SPITCHING': {'frontend_react', 'backend_spring', 'ai_ml', 'product_planning_design', 'cloud_devops', 'other'},
           'MANSUMUGANG': {'backend_node', 'cloud_devops', 'product_planning_design', 'other'}}
MENTION = {
    'INFLUY': 'The conversation explicitly deals with INFLUY/인플루이 or its domain: influencers or sellers selling to buyers, seller talk box (톡박스), FAQ cards, question categories, market links, CEOS.',
    'ON': 'The conversation explicitly deals with ON or its domain: a community app for exchange students (교환학생), accompany (동행) recruiting, chat rooms, country list, studying abroad.',
    'SPITCHING': 'The conversation explicitly deals with SPitching/스피칭 or its domain: presentation practice, eye contact, gestures, filler words, speech fluency, script similarity, graduation project (졸프), or existing interview/presentation AI services.',
    'MANSUMUGANG': 'The conversation explicitly deals with 만수무강 or its domain: a service for elderly people (노인, 어르신, 시니어).',
}


def questions():
    q = {'category': {'type': 'choice',
                      'instructions': 'What is this conversation mainly for? Pick the single best category from the title, user messages, assistant excerpts and code identifiers.',
                      'criteria': CATEGORIES},
         'is_project_work': {'type': 'noul',
                             'instructions': 'The user is actually building, planning, designing or debugging a specific team or product project in this conversation (not studying a topic, not doing course homework, not writing about past work).'},
         'is_writing_about_experience': {'type': 'noul',
                                         'instructions': 'The conversation writes or polishes text about past experience: a cover letter (자소서), application answer, resume, portfolio or interview answer.'},
         'tech_area': {'type': 'choice', 'instructions': 'Which technical area does this conversation mostly deal with?',
                       'criteria': TECH}}
    for p in PROJECTS:
        q['mentions_' + p] = {'type': 'noul', 'instructions': MENTION[p]}
    return q


# ---------- data ----------

def load_items():
    rows = [json.loads(l) for l in (INDEX / 'CLASSIFICATION_FINAL.jsonl').read_text().splitlines()]
    dev = [r for r in rows if r['final_decision'] == 'DEV']
    if len(dev) != EXPECTED_DEV:
        sys.exit(f'DEV count {len(dev)} != {EXPECTED_DEV}; stop.')
    db = sqlite3.connect(f"file:{INDEX / 'search.sqlite3'}?mode=ro", uri=True)
    dates = dict(db.execute('SELECT l.classification_id, c.created_at FROM conversation_locations l '
                            'JOIN conversations c ON c.id = l.conversation'))
    db.close()
    items = [{'id': r['classification_id'], 'title': r['title'], 'created_at': dates.get(r['classification_id']),
              **r['raw_location']} for r in dev]
    items.sort(key=lambda x: (x['created_at'] or '', x['id']))
    return items


def manifest():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / 'manifest.json'
    if not path.exists():
        items = load_items()
        path.write_text(json.dumps({'source': str(INDEX.relative_to(ROOT)), 'order': 'created_at asc',
                                    'count': len(items), 'items': items}, ensure_ascii=False, indent=2) + '\n')
    return json.loads(path.read_text())['items']


def pointer_get(v, p):
    for k in p.split('/')[1:] if p else []:
        k = k.replace('~1', '/').replace('~0', '~')
        v = v[int(k)] if isinstance(v, list) else v[k]
    return v


_FILE = {'path': None, 'data': None}


def conversation_messages(item):
    from chatgpt_body_review import sha

    from tools.chatgpt_pipeline.core import text_parts
    if _FILE['path'] != item['path']:
        raw = ROOT / item['path']
        if sha(raw) != item['sha256']:
            sys.exit('raw file hash changed; stop.')
        _FILE.update(path=item['path'], data=json.loads(raw.read_text()))
    conv = pointer_get(_FILE['data'], item['pointer'])
    mapping = conv.get('mapping') or {}
    chain, node, seen = [], conv.get('current_node'), set()
    while node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get('parent')
    out = []
    for n in reversed(chain):
        m = n.get('message')
        if not isinstance(m, dict) or (m.get('metadata') or {}).get('is_visually_hidden_from_conversation'):
            continue
        role = (m.get('author') or {}).get('role')
        if role not in ('user', 'assistant'):
            continue
        text = '\n'.join(p for _, p in text_parts(m.get('content'))).strip()
        if text:
            out.append((role, text))
    return out


IDENT = re.compile(r"\buse[A-Z]\w+|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b|"
                   r"(?:src|app|components|pages|hooks|api|services|routes|store|public)/[\w./\[\]-]+|"
                   r"\b[\w-]+\.(?:tsx?|jsx?|py|java|sql|css|json|cpp|h)\b|from\s+['\"]([^'\"]+)['\"]")


def build_state(item, messages):
    from tools.chatgpt_pipeline.core import redact
    flat = lambda t: ' '.join(t.split())
    users, total = [], 0
    for role, text in messages:
        if role != 'user' or total >= USER_TOTAL:
            continue
        piece = redact(flat(text))[:min(USER_EACH, USER_TOTAL - total)]
        users.append(piece)
        total += len(piece)
    assists = [redact(flat(t))[:ASSIST_EACH] for r, t in messages if r == 'assistant'][:ASSIST_N]
    idents = []
    for _, text in messages:
        for m in IDENT.finditer(text):
            tok = m.group(1) or m.group(0)
            if tok not in idents:
                idents.append(tok)
    idents = [redact(t) for t in idents[:IDENT_MAX]]
    return {'title': item['title'], 'user_messages': users, 'assistant_excerpts': assists, 'code_identifiers': idents}


# ---------- local signals ----------

def compile_keywords():
    kw = json.loads((OUT / 'keywords.json').read_text())

    def pat(term):
        if term.isascii():
            return re.compile(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', re.I)
        return re.compile(re.escape(term), re.I)
    return kw['version'], {p: {lvl: [(t, pat(t)) for t in terms] for lvl, terms in d.items()}
                           for p, d in kw['projects'].items()}


def keyword_evidence(text, compiled):
    ev = {}
    for p, levels in compiled.items():
        hits = {lvl: sorted({t for t, rx in pats if rx.search(text)}) for lvl, pats in levels.items()}
        if not hits['high']:
            hits['shared'] = []  # shared terms only count alongside a high hit of the same project
        score = len(hits['high']) + len(hits['medium']) // 2
        ev[p] = {'score': score, **hits}
    return ev


def month_index(ym):
    y, m = map(int, ym.split('-'))
    return y * 12 + m - 1


def date_weight(project, ym):
    if not ym:
        return 1.0, 'unknown_date'
    start, end = map(month_index, PERIODS[project])
    cur = month_index(ym)
    if cur < start:
        return 0.05, 'before_start'
    if cur <= end:
        return 1.0, 'in_period'
    after = cur - end
    return (0.6, 'after_1_2m') if after <= 2 else (0.3, 'after_3_6m') if after <= 6 else (0.1, 'after_6m_plus')


# ---------- decision ----------

def decide(answers, ev, ym):
    cat = answers['category']
    probs = dict(cat['probabilities'])
    weights = {p: date_weight(p, ym) for p in PROJECTS}
    adj = {c: probs.get(c, 0.0) * (weights[c][0] if c in weights else 1.0) for c in CATEGORIES}
    s = sum(adj.values()) or 1.0
    q = {c: v / s for c, v in adj.items()}
    noul = lambda k: float(answers[k]['noul'])
    work, writing = noul('is_project_work'), noul('is_writing_about_experience')
    mentions = {p: noul('mentions_' + p) for p in PROJECTS}
    tech = answers['tech_area']
    best = max(q, key=q.get)
    proj = max(PROJECTS, key=lambda p: q[p])

    failed = []
    if q[proj] < T_PROJECT or best != proj:
        failed.append(f'score {q[proj]:.2f} < {T_PROJECT} or not top')
    if work < T_WORK:
        failed.append(f'is_project_work {work:.2f} < {T_WORK}')
    if writing >= T_WRITING:
        failed.append(f'is_writing_about_experience {writing:.2f} >= {T_WRITING}')
    if not (mentions[proj] >= T_MENTION or ev[proj]['score'] >= 1):
        failed.append('no evidence (mention noul and keywords both weak)')
    others = [p for p in PROJECTS if p != proj and (mentions[p] >= T_MENTION or ev[p]['score'] >= 2)]
    if others:
        failed.append('conflict with ' + ','.join(others))
    tech_p = tech['probabilities'].get(tech['choice'], 0)
    if tech['choice'] not in TECH_OK[proj] and tech_p >= T_TECH and mentions[proj] < T_MENTION:
        failed.append(f"tech_area {tech['choice']} inconsistent")
    if weights[proj][1] in ('before_start', 'after_6m_plus'):
        failed.append('date ' + weights[proj][1])

    candidate = None
    if q[proj] >= 0.2 or ev[proj]['score'] >= 1 or mentions[proj] >= 0.5:
        candidate = {'project': proj, 'score': round(q[proj], 3), 'failed': failed}

    if writing >= T_WRITING:  # purpose first: writing about experience is never project work
        final = 'COVER_LETTER' if q['COVER_LETTER'] >= q['INTERVIEW_PREP'] else 'INTERVIEW_PREP'
        conf = max(writing, q[final])
    elif not failed:
        final, conf, candidate = proj, q[proj], None
    else:
        rest = {c: v for c, v in q.items() if c not in PROJECTS}
        rs = sum(rest.values()) or 1.0
        top = max(rest, key=rest.get)
        conf = rest[top] / rs
        final = top if conf >= T_NONPROJECT else 'UNCLEAR'
    return {'final_category': final, 'confidence': round(conf, 3), 'project_candidate': candidate,
            'adjusted_probabilities': {c: round(v, 4) for c, v in q.items()},
            'date_weights': {p: {'weight': w, 'phase': ph} for p, (w, ph) in weights.items()},
            'nouls': {'is_project_work': work, 'is_writing_about_experience': writing,
                      **{'mentions_' + p: v for p, v in mentions.items()}},
            'tech_area': {'choice': tech['choice'], 'probability': tech_p}}


# ---------- API ----------

def load_key():
    key = None
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition('=')
            if name.strip() == 'TYPESAFE_API_KEY':
                key = value.strip().strip('"\'')
    if not key:
        sys.exit(f'no API key in {ENV_FILE}')
    return key


def call_api(body, key):
    import urllib.error
    import urllib.request
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    for attempt in range(4):
        req = urllib.request.Request(API_URL, data=data, method='POST', headers={
            'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read()), attempt
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


def run(items, upto):
    wall0 = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    key = load_key()
    kw_version, compiled = compile_keywords()
    qs = questions()
    path = OUT / 'results.jsonl'
    done = {json.loads(l)['id'] for l in path.read_text().splitlines()} if path.exists() else set()
    order = sorted(enumerate(items[:upto], 1), key=lambda t: (t[1]['path'], t[0]))  # read each raw file once
    req_ms, prep_ms, retries = [], [], 0
    for seq, item in order:
        if item['id'] in done:
            continue
        t0 = time.perf_counter()
        msgs = conversation_messages(item)
        state = build_state(item, msgs)
        ev = keyword_evidence(item['title'] + '\n' + '\n'.join(t for _, t in msgs), compiled)
        body = {'model': MODEL, 'state': state, 'questions': qs}
        t1 = time.perf_counter()  # request time = serialize + send + server + receive + parse
        resp, attempt = call_api(body, key)
        t2 = time.perf_counter()
        retries += attempt
        ym = (item['created_at'] or '')[:7] or None
        row = {'seq': seq, 'id': item['id'], 'title': item['title'], 'created_ym': ym,
               **decide(resp['answers'], ev, ym),
               'keyword_evidence': {p: e for p, e in ev.items() if e['score'] or e['medium'] or e['high']},
               'jev_category_probabilities': resp['answers']['category']['probabilities'],
               'model': resp.get('model'), 'usage': resp.get('usage'),
               'sent_chars': len(json.dumps(state, ensure_ascii=False)),
               'request_ms': round((t2 - t1) * 1000, 1), 'prep_ms': round((t1 - t0) * 1000, 1),
               'rule_version': RULE_VERSION, 'keywords_version': kw_version}
        with path.open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        req_ms.append(row['request_ms'])
        prep_ms.append(row['prep_ms'])
        print(f"[{len(req_ms)}] #{seq} {row['final_category']:<17} conf={row['confidence']:.2f} "
              f"req={row['request_ms']:.0f}ms  {item['title'][:40]}", flush=True)
    wall = time.perf_counter() - wall0
    timing = {'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
              'wall_seconds_total': round(wall, 1), 'calls': len(req_ms), 'retries': retries,
              'request_seconds_sum': round(sum(req_ms) / 1000, 1), 'prep_seconds_sum': round(sum(prep_ms) / 1000, 1)}
    if req_ms:
        s = sorted(req_ms)
        timing.update(request_ms_avg=round(statistics.mean(s), 1), request_ms_p50=s[len(s) // 2],
                      request_ms_p95=s[int(len(s) * 0.95) - 1], request_ms_max=s[-1])
    runs = json.loads((OUT / 'timing.json').read_text()) if (OUT / 'timing.json').exists() else []
    runs.append(timing)
    (OUT / 'timing.json').write_text(json.dumps(runs, indent=2) + '\n')
    print('timing:', json.dumps(timing))
    report()


def report():
    path = OUT / 'results.jsonl'
    if not path.exists():
        sys.exit('no results yet')
    rows = sorted((json.loads(l) for l in path.read_text().splitlines()), key=lambda r: r['seq'])
    head = 'seq\tid\tcreated_ym\tfinal_category\tconfidence\tproject_candidate\tcandidate_score\tfailed\tkeyword_hits\ttitle'
    lines = [head]
    for r in rows:
        c = r['project_candidate'] or {}
        hits = '; '.join(f"{p}:{','.join(e['high'][:5])}" for p, e in r['keyword_evidence'].items() if e['high'])
        lines.append('\t'.join(map(str, [r['seq'], r['id'], r['created_ym'], r['final_category'], r['confidence'],
                                         c.get('project', ''), c.get('score', ''), ' | '.join(c.get('failed', [])),
                                         hits, r['title']])))
    (OUT / 'classification.tsv').write_text('\n'.join(lines) + '\n')
    review = [lines[0]] + [l for l in lines[1:] if l.split('\t')[5]]
    (OUT / 'review_project_candidates.tsv').write_text('\n'.join(review) + '\n')
    print('rows:', len(rows), '| by category:', dict(collections.Counter(r['final_category'] for r in rows).most_common()))
    print('project candidates (not assigned):',
          dict(collections.Counter(r['project_candidate']['project'] for r in rows if r['project_candidate'])))
    print('tables:', OUT / 'classification.tsv', '|', OUT / 'review_project_candidates.tsv')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if cmd == 'report':
        report()
        return
    items = manifest()
    if cmd == 'run':
        run(items, int(sys.argv[2]) if len(sys.argv) > 2 else len(items))
    elif cmd == 'preview':
        item = items[int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 0]
        print(json.dumps({'model': MODEL, 'state': build_state(item, conversation_messages(item)),
                          'questions': questions()}, ensure_ascii=False, indent=2))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
