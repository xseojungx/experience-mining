"""Lossless, deterministic legacy projection; no original writes or semantic repair.

Legacy provenance points to JSON Pointers in the three explicit input files. A
pointer into analysis JSON is NOT a recovered position in a GitHub/Git source.
Adapter content digests are NOT historical analysis revision numbers.
"""
import json
from pathlib import Path

from .analysis_contract import CONTRACT_VERSION, SPEC_VERSION, validate_bundle
from .analysis_store import ADAPTER_VERSION, digest
from .audit_state import audit_legacy, safe_path, sha256

DEFAULT_INPUTS = ['state/influy-pilot-candidates.json','state/influy-pilot-assessment.json',
                  'state/influy-pilot-verification-queue.json']


def pointer(value):
    return str(value).replace('~','~0').replace('/','~1')


def adapt_legacy(root, paths=None):
    root = Path(root)
    paths = DEFAULT_INPUTS if paths is None else paths
    if len(paths)!=3 or len(set(paths))!=3:
        raise ValueError('candidate, assessment and review paths required in order')
    files = [safe_path(root,p) for p in paths]
    raw = [json.loads(p.read_text()) for p in files]
    candidates, assessment, queue = raw
    findings = audit_legacy(*raw)
    fatal = {'legacy_shape','duplicate','dangling','state','candidate_set','score','score_evidence',
             'rubric','analysis_version','legacy_claim_id','publication'}
    if any(i['code'] in fatal for i in findings):
        raise ValueError('legacy structural failure: '+str([i for i in findings if i['code'] in fatal]))
    # Missing metadata is explicit, whereas malformed collections are rejected.
    if not candidates or not isinstance(assessment.get('analysis_version'),str):
        raise ValueError('legacy candidates and analysis_version required')
    for c in candidates:
        if c.get('published') is not False or c.get('independent_verification')!='pending':
            raise ValueError('adapter accepts only unpublished pending legacy snapshots')
    if any(r.get('status')!='pending' for r in queue['items']):
        raise ValueError('legacy review status must remain pending')
    hashes = [sha256(f) for f in files]
    def provenance(index, ptr, original_id=None):
        return {'path':paths[index],'sha256':hashes[index],'json_pointer':ptr,'original_id':original_id}
    bundle = {'contract_version':CONTRACT_VERSION,'spec_version':SPEC_VERSION,
              'analysis_version':assessment['analysis_version'],
              **{t:[] for t in ['candidates','claims','evidence','assessments','verifications','notes']}}
    edges,locations = set(),[]
    def edge(fk,fi,relation,tk,ti):
        edges.add((fk,fi,relation,tk,ti))
    def evidence(record, index, ptr):
        if not isinstance(record,dict):
            raise ValueError('legacy evidence object required')
        ident = 'legacy-evidence:'+digest([paths[index],ptr])
        raw_path = record.get('raw_path',record.get('source'))
        # sha256/content_sha256 in a legacy code excerpt refers to content, not
        # the enclosing raw JSON. Only the explicit source_hashes ledger maps it.
        snapshot = assessment.get('source_hashes',{}).get(raw_path)
        item = {'id':ident,'source_id':None,'origin':None,'location':record.get('url'),
                'raw_path':raw_path,'sha256':snapshot,'version':record.get('sha',record.get('original_commit')),
                'author':record.get('author'),'created_at':record.get('created_at'),'collected_at':record.get('collected_at'),
                'provenance':provenance(index,ptr,record.get('id')),'legacy_payload':record}
        bundle['evidence'].append(item)
        locations.append({'evidence_id':ident,'raw_path':raw_path,'snapshot_sha256':snapshot,
                          'selector':{k:record[k] for k in ('id','url','sha','original_commit','path','lines') if k in record}})
        return ident
    claim_ids = set()
    claim_payloads = {}
    def claim(record, ident, index, ptr, explicit_kind=None):
        if ident in claim_ids:
            raise ValueError('duplicate projected claim ID')
        claim_ids.add(ident)
        claim_payloads[ident] = record
        eids = [evidence(e,index,ptr+'/evidence/'+str(i)) for i,e in enumerate(record.get('evidence',[]))]
        item = {'id':ident,'candidate_id':record.get('candidate_id'),'revision':None,
                'text':record.get('text'),'kind':record.get('classification',explicit_kind),
                'author':None,'actor':None,'evidence_ids':eids,'core_aspects':None,
                'provenance':provenance(index,ptr,record.get('id')),'legacy_payload':record}
        for field in ('uncertainty','alternative_explanations'):
            if field in record:
                item[field] = record[field]
        # Source speaker/author is preserved in legacy_payload; its meaning is not
        # silently changed into the C01 analysis author.
        if record.get('classification')=='user_confirmation':
            item.update(answer_verbatim=record.get('verbatim'),confirmed_at=record.get('confirmed_at'),question=None)
        for e in eids:
            edge('claim',ident,'legacy_unclassified','evidence',e)
        bundle['claims'].append(item)
    for ident,entry in assessment['claim_index'].items():
        claim(entry['claim'],ident,1,'/claim_index/'+pointer(ident)+'/claim')
    ids = {c['id'] for c in candidates}
    for i,c in enumerate(candidates):
        ident = c['id']
        bundle['candidates'].append({'id':ident,'project':c['project'],'state':c['state'],
            'revision':None,'author':None,'disposition':None,'history':None,'decision_ids':None,
            'claim_ids':[],'assessment_ids':[],'verification_ids':[],'note_ids':[],
            'provenance':provenance(0,'/'+str(i),ident),'legacy_payload':c})
        for j,e in enumerate(c.get('evidence',[])):
            evidence(e,0,f'/{i}/evidence/{j}')
        for record in c.get('recorded_facts',[]):
            if record['id'] not in claim_ids:
                raise ValueError('candidate claim absent from index')
            if claim_payloads[record['id']] != record:
                raise ValueError('conflicting candidate/index claim: '+record['id'])
            edge('candidate',ident,'candidate_claim','claim',record['id'])
        for key in ['recorded_fact','ai_inference']:
            values = c.get(key)
            if values is None:
                continue
            for j,value in enumerate(values if isinstance(values,list) else [values]):
                record = {'text':value} if isinstance(value,str) else value
                cid = record.get('id','legacy-claim:'+digest([paths[0],i,key,j]))
                ptr=f'/{i}/{key}'+('/'+str(j) if isinstance(values,list) else '')
                # Existing IDs retain their canonical index form; other forms stay
                # losslessly preserved in the candidate's original payload.
                if cid in claim_ids and claim_payloads[cid] != record:
                    raise ValueError('conflicting representations for claim ID: '+cid)
                if cid not in claim_ids:
                    claim(record,cid,0,ptr,explicit_kind={'recorded_fact':'recorded_fact','ai_inference':'ai_inference'}[key])
                edge('candidate',ident,'candidate_claim','claim',cid)
        for cid in c.get('investigated_claim_ids',[]):
            if cid not in claim_ids:
                raise ValueError('investigated claim missing')
            edge('candidate',ident,'candidate_claim','claim',cid)
        uc=c.get('user_confirmation')
        if isinstance(uc,dict):
            if uc.get('id') not in claim_ids:
                raise ValueError('confirmation absent from index')
            edge('candidate',ident,'candidate_claim','claim',uc['id'])
    for i,a in enumerate(assessment['assessments']):
        ident = 'legacy-assessment:'+a['candidate_id']
        item = {**a,'id':ident,'revision':assessment.get('assessment_version'),'author':None,
                'provenance':provenance(1,f'/assessments/{i}',a.get('id')),'legacy_payload':a}
        bundle['assessments'].append(item)
        edge('candidate',a['candidate_id'],'candidate_assessment','assessment',ident)
        for cid in {cid for s in a['scores'] for cid in s['evidence_claim_ids']}:
            edge('assessment',ident,'score_claim','claim',cid)
            edge('candidate',a['candidate_id'],'candidate_claim','claim',cid)
    for i,v in enumerate(queue['items']):
        if v['candidate_id'] not in ids:
            raise ValueError('review candidate missing')
        ident = 'legacy-verification:'+v['candidate_id']
        bundle['verifications'].append({**v,'id':ident,'reviewer':None,'reviewed_at':None,
            'revision_digest':None,'claim_ids':v['source_claim_ids'],'evidence_ids':[],
            'issues':[], 'issues_inventory_complete':False,
            'provenance':provenance(2,f'/items/{i}',v.get('id')),'legacy_payload':v})
        edge('candidate',v['candidate_id'],'candidate_verification','verification',ident)
        for cid in v['source_claim_ids']:
            edge('verification',ident,'review_claim','claim',cid)
            edge('candidate',v['candidate_id'],'candidate_claim','claim',cid)
    for c in bundle['candidates']:
        for kind,field in [('claim','claim_ids'),('assessment','assessment_ids'),('verification','verification_ids')]:
            c[field]=sorted(e[4] for e in edges if e[:3]==('candidate',c['id'],'candidate_'+kind))
    # candidate membership is a relationship, not inferred authorship/ownership.
    # Preserve all three inputs, including unmapped metadata and old input hashes.
    diagnostics = {'legacy_audit':findings,'c01_projection':validate_bundle(bundle),
                   'unknowns':['analysis authors','actors','historical revisions and transitions',
                     'claim ownership where absent','source identity/acquisition metadata',
                     'evidence polarity','exact source selectors where absent',
                     'independent reviewer and current digest','issue inventory','note publication eligibility']}
    return {'adapter_version':ADAPTER_VERSION,'grade':'legacy_incomplete','bundle':bundle,
            'edges':[list(e) for e in sorted(edges)],'locations':locations,'diagnostics':diagnostics,
            'legacy_inputs':[{'provenance':provenance(i,''),'payload':value} for i,value in enumerate(raw)]}
