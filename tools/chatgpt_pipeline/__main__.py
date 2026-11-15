import argparse
import json

from .core import ROOT, SPEC, connect, extract, file_hash, initialize, inventory, run_lock, safe, search, titles, trace
from .github import discover_links, github_index, relations
from .report import export_compatibility, summary, validate, write_json


def main():
    p=argparse.ArgumentParser(description='Isolated ChatGPT pipeline. No network, source writes or operational DB writes.')
    p.add_argument('--output',default='results/chatgpt-c-2026-11-15')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('run');sub.add_parser('titles');sub.add_parser('extract');sub.add_parser('github');sub.add_parser('link');sub.add_parser('validate');sub.add_parser('export');sub.add_parser('summary')
    q=sub.add_parser('search');q.add_argument('query',nargs='?',default='');q.add_argument('--substring',action='store_true');q.add_argument('--account');q.add_argument('--conversation-id');q.add_argument('--message-id');q.add_argument('--since');q.add_argument('--until');q.add_argument('--project');q.add_argument('--pr',type=int);q.add_argument('--repository');q.add_argument('--limit',type=int,default=20)
    t=sub.add_parser('trace');t.add_argument('segment_id')
    args=p.parse_args();output=safe(ROOT,args.output)
    if output.parent!=ROOT/'results' or not output.name.startswith('chatgpt-c-'):raise ValueError('dedicated results/chatgpt-c-* output required')
    if args.command in ('search','trace','summary'):
        if args.command=='summary':print(json.dumps(summary(output),ensure_ascii=False,indent=2));return
        db=connect(output/'search.sqlite3',readonly=True)
        try:
            value=trace(ROOT,db,args.segment_id) if args.command=='trace' else search(db,args.query,substring=args.substring,account=args.account,conversation_id=args.conversation_id,message_id=args.message_id,since=args.since,until=args.until,project=args.project,pr_number=args.pr,repository=args.repository,limit=args.limit)
            print(json.dumps(value,ensure_ascii=False,indent=2))
        finally:db.close()
        return
    authorization=json.loads((output/'AUTHORIZATION.json').read_text())
    if authorization.get('task_id')!='CG-C-20261115' or authorization.get('approved') is not True:raise ValueError('explicit task authorization required')
    if authorization.get('output')!=args.output:raise ValueError('output differs from authorization')
    for path,expected in SPEC.items():
        if file_hash(ROOT/path)!=expected:raise ValueError('canonical spec drift')
    with run_lock(output):
        initialize(output)
        if args.command in ('run','titles'):
            files=inventory(ROOT,output);write_json(output/'RAW_INVENTORY.json',files)
            failures=titles(ROOT,output,files);write_json(output/'TITLE_FAILURES.json',failures)
            print('title pass complete; failed files:',len(failures),flush=True)
        else:files=json.loads((output/'RAW_INVENTORY.json').read_text())
        if args.command in ('run','extract'):
            failures=extract(ROOT,output,files);write_json(output/'BODY_FAILURES.json',failures)
            print('selective body extraction complete; failed files:',len(failures),flush=True)
        if args.command in ('run','github'):
            result=github_index(ROOT,output,output/'github-operational-snapshot.sqlite3');write_json(output/'GITHUB_INPUTS.json',result)
            print('GitHub local index complete; failed sources:',len(result['failures']),flush=True)
        if args.command in ('run','link'):
            result=discover_links(output);relations(output);write_json(output/'LINK_RUN_RESULT.json',result)
            print('bidirectional search complete',flush=True)
        if args.command in ('run','export'):
            print(json.dumps(export_compatibility(output)),flush=True)
        if args.command in ('run','validate'):
            gh=json.loads((output/'GITHUB_INPUTS.json').read_text()) if (output/'GITHUB_INPUTS.json').exists() else {'sources':[]}
            result=validate(ROOT,output,files,github_sources=gh['sources']);print(json.dumps(result),flush=True)
            if not result['clean']:raise SystemExit(1)
        write_json(output/'SUMMARY.json',summary(output))


if __name__=='__main__':main()
