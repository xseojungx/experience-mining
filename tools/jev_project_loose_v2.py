#!/usr/bin/env python3
"""PRELIMINARY looser re-run, version project-loose-v2.

Same rules as tools/jev_project_loose.py (project-loose-v1), plus four fixes:
  1. keywords kw-v2: '시니어' only counts for 만수무강 together with 노인/어르신/고령자/만수무강
     (v1 matched '시니어 개발자').
  2. size fallback: after per-message caps of 4,000 and 1,500 chars, a final budget mode
     keeps every user message (up to 1,000 chars each) and assistant messages (up to 300 chars
     each) within 40,000 chars total, in conversation order. The mode used is recorded.
  3. keyword hits are counted once regardless of case ('Express' and 'express' were two hits).
  4. keyword evidence alone does not assign a project to a conversation more than 6 months after
     the project ended, unless Jev's mention noul for that project is >= 0.5.
Reuses v1/loose-v1 code read-only; writes only to results/jev-project-loose-v2-2026-11-15/.

  python3 tools/jev_project_loose_v2.py run [N] | report

Removal: delete this file and results/jev-project-loose-v2-2026-11-15/.
"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('loose_v1', ROOT / 'tools/jev_project_loose.py')
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)
v1 = L.v1

L.OUT = ROOT / 'results/jev-project-loose-v2-2026-11-15'
L.RULE_VERSION = 'project-loose-v2'
KEYWORDS = L.OUT / 'keywords.json'
BUDGET, USER_EACH, ASSIST_EACH = 40000, 1000, 300
L.FALLBACK_SHRINK = [None, 4000, 1500, 'budget']


def compile_keywords():
    kw = json.loads(KEYWORDS.read_text())

    def pat(term):
        if term.isascii():
            return re.compile(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', re.I)
        return re.compile(re.escape(term), re.I)
    return kw['version'], {p: {lvl: [(t, pat(t)) for t in terms] for lvl, terms in d.items()}
                           for p, d in kw['projects'].items()}


_full_state_v1 = L.full_state


def full_state(item, messages, cap):
    if cap != 'budget':
        return _full_state_v1(item, messages, cap)
    from tools.chatgpt_pipeline.core import redact
    out, total = [], 0
    for role, text in messages:
        piece = redact(' '.join(text.split()))[:USER_EACH if role == 'user' else ASSIST_EACH]
        if total + len(piece) > BUDGET:
            if role == 'assistant':
                continue  # keep room for later user questions
            piece = piece[:max(BUDGET - total, 0)]
            if not piece:
                break
        out.append({'role': role, 'text': piece})
        total += len(piece)
    return {'title': item['title'], 'messages': out, 'note': 'shortened to fit the model input limit'}


def keyword_evidence(text, compiled):
    ev = {}
    for p, levels in compiled.items():
        hits = {}
        for lvl, pats in levels.items():
            seen = {}
            for t, rx in pats:
                if rx.search(text):
                    seen.setdefault(t.lower(), t)
            hits[lvl] = sorted(seen.values())
        if not hits['high']:
            hits['shared'] = []
        ev[p] = {'score': len(hits['high']) + len(hits['medium']) // 2, **hits}
    return ev


_decide_v1 = L.decide


def decide(answers, ev, ym):
    stale = [p for p in L.PROJECTS if v1.date_weight(p, ym)[1] == 'after_6m_plus'
             and float(answers['mentions_' + p]['noul']) < 0.5 and ev[p]['score'] >= 1]
    if stale:  # drop keyword-only evidence for long-ended projects, keep it on record
        ev = {p: ({**e, 'score': 0} if p in stale else e) for p, e in ev.items()}
    out = _decide_v1(answers, ev, ym)
    if stale:
        out['stale_keyword_projects'] = stale
    return out


v1.compile_keywords = compile_keywords
v1.keyword_evidence = keyword_evidence
L.full_state = full_state
L.decide = decide

if __name__ == '__main__':
    L.main()
