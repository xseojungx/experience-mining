"""C02 additive storage. Path APIs remain temp-only; connection APIs require an
explicit caller-owned transaction. C02-2 migration runner owns production safety.
No queue integration, review execution or publication.
"""
import ast
import hashlib
import json
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .analysis_contract import validate_bundle
from .audit_state import safe_path, sha256

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'state/migrations/002_analysis.sql'
BASE = ROOT / 'state/schema.sql'
TABLES = ('candidates', 'claims', 'evidence', 'assessments', 'verifications', 'notes')
KINDS = ('candidate', 'claim', 'evidence', 'assessment', 'verification', 'note')
ADAPTER_VERSION = 'c02-1-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sandbox_path(path):
    path = Path(path).absolute()
    resolved = path.resolve()
    roots = {Path('/tmp').resolve(), Path(tempfile.gettempdir()).resolve()}
    if (resolved.is_relative_to(ROOT) or not any(resolved.is_relative_to(p) for p in roots)
            or any(p.is_symlink() for p in (path, *path.parents))):
        # /tmp itself is a macOS symlink; callers use a resolved TemporaryDirectory.
        raise ValueError('writes require a resolved system-temp sandbox outside workspace')
    return resolved


@contextmanager
def connection(path):
    path = sandbox_path(path)
    db = sqlite3.connect(path)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        yield db
    finally:
        db.close()


def statements(sql):
    pending = ''
    for line in sql.splitlines(True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise ValueError('incomplete SQL')


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('foreign key violation')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate(path, *, fail_after=None):
    """Apply only to a pre-existing legacy-schema temporary DB, atomically."""
    path = sandbox_path(path)
    if not path.is_file():
        raise ValueError('existing isolated database required')
    with connection(path) as db:
        with transaction(db):
            return migrate_connection(db, fail_after=fail_after)


def migrate_connection(db, *, fail_after=None):
    """Apply version 2 in a caller-owned transaction; never commit or open a path."""
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('explicit transaction with foreign_keys=ON required')
    expected = sqlite3.connect(':memory:')
    try:
        expected.executescript(BASE.read_text())
        # The original collector registered PRs in its own table, outside
        # schema.sql. Preserve and validate that exact historical DDL too.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='pr_inventory'").fetchone():
            tree = ast.parse((ROOT / 'tools/collect_prs.py').read_text())
            definitions = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                           and isinstance(node.value, str)
                           and node.value.strip().startswith('CREATE TABLE IF NOT EXISTS pr_inventory (')]
            if len(definitions) != 1:
                raise ValueError('collector inventory schema definition ambiguous')
            expected.executescript(definitions[0])
        baseline = list(expected.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"))
    finally:
        expected.close()
    actual = list(db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ana_%' AND name NOT LIKE 'ctl_%' ORDER BY name"))
    if actual != baseline:
        raise ValueError('legacy schema differs; stop for migration review')
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='ana_migrations'").fetchone():
        row = db.execute('SELECT version,sha256,base_sha256 FROM ana_migrations').fetchall()
        if row != [(2, sha256(MIGRATION), sha256(BASE))]:
            raise ValueError('migration history/checksum mismatch')
        # Detect missing/tampered extension objects as well as history drift.
        probe = sqlite3.connect(':memory:')
        try:
            probe.executescript(BASE.read_text()+MIGRATION.read_text())
            wanted = list(probe.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'ana_%' ORDER BY name"))
            found = list(db.execute("SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'ana_%' ORDER BY name"))
            if wanted != found:
                raise ValueError('extension schema drift')
        finally:
            probe.close()
        return False
    for i, sql in enumerate(statements(MIGRATION.read_text()), 1):
        db.execute(sql)
        if fail_after == i:
            raise RuntimeError('synthetic migration interruption')
    db.execute('INSERT INTO ana_migrations(version,name,sha256,base_sha256) VALUES(2,?,?,?)',
               ('002_analysis', sha256(MIGRATION), sha256(BASE)))
    return True


def restore_sandbox(path):
    """Destructive DOWN for temporary verification only; never production recovery."""
    with connection(path) as db:
        with transaction(db):
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'ana_%'").fetchall():
                db.execute('DROP VIEW "'+name+'"')
            for name in ('ana_issues','ana_locations','ana_edges','ana_run_revisions','ana_revisions',
                         'ana_objects','ana_inputs','ana_runs','ana_artifacts','ana_migrations'):
                db.execute('DROP TABLE '+name)


def artifact(root, path, version):
    file = safe_path(Path(root), path)
    if not isinstance(version, str) or not version.strip():
        raise ValueError('artifact version required')
    return {'path': path, 'sha256': sha256(file), 'version': version}


def _artifact(db, row):
    db.execute('INSERT INTO ana_artifacts(path,sha256,version) VALUES(?,?,?) ON CONFLICT DO NOTHING',
               (row['path'], row['sha256'], row['version']))
    return db.execute('SELECT id FROM ana_artifacts WHERE path=? AND sha256=? AND version=?',
                      (row['path'], row['sha256'], row['version'])).fetchone()[0]


def contract_package(bundle):
    errors = validate_bundle(bundle)
    if errors:
        raise ValueError('C01 contract rejected: '+canonical(errors))
    edges = []
    def edge(fk, fi, rel, tk, ti):
        edges.append([fk,fi,rel,tk,ti])
    for c in bundle['candidates']:
        for _table, kind in zip(TABLES[1:], KINDS[1:]):
            if kind == 'evidence':
                continue
            field = {'claim':'claim_ids','assessment':'assessment_ids','verification':'verification_ids','note':'note_ids'}[kind]
            for ident in c[field]:
                edge('candidate',c['id'],'candidate_'+kind,kind,ident)
    for c in bundle['claims']:
        # C01 evidence_ids encodes association, not polarity. Explicit input required
        # for supports/refutes; context is the non-polar C01 association convention.
        for e in c['evidence_ids']:
            edge('claim',c['id'],'context','evidence',e)
    for a in bundle['assessments']:
        for ident in sorted({x for s in a['scores'] for x in s['evidence_claim_ids']}):
            edge('assessment',a['id'],'score_claim','claim',ident)
    for v in bundle['verifications']:
        for field,kind in [('claim_ids','claim'),('evidence_ids','evidence')]:
            for ident in v[field]:
                edge('verification',v['id'],'review_'+kind,kind,ident)
    locations = [{'evidence_id': e['id'], 'raw_path': e['raw_path'], 'snapshot_sha256': e['sha256'],
                  'selector': {'location':e['location'],'version':e['version'],'source_id':e['source_id']}}
                 for e in bundle['evidence']]
    return {'adapter_version':ADAPTER_VERSION,'grade':'c01_valid','bundle':bundle,
            'diagnostics':[], 'edges':edges,'locations':locations}


def store(path, root, package, inputs, result_path, *, predecessors=None, fail_after=None):
    """C02-1-compatible isolated store; production uses the explicit runner."""
    with connection(path) as db:
        with transaction(db):
            return store_connection(db, root, package, inputs, result_path,
                                    predecessors=predecessors, fail_after=fail_after)


def store_connection(db, root, package, inputs, result_path, *, predecessors=None, fail_after=None):
    """Atomic run registration; exact replay no-op. Files checked before opening DB.

    inputs: [{path,sha256,version}]. result_path contains this exact package JSON.
    predecessors: explicit {(kind,id): previous content digest}; never inferred.
    """
    if not db.in_transaction or db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise ValueError('explicit transaction with foreign_keys=ON required')
    if package.get('adapter_version') != ADAPTER_VERSION:
        raise ValueError('adapter version mismatch')
    bundle = package['bundle']
    if package['grade'] == 'c01_valid':
        expected = contract_package(bundle)
        # Added explicit graph edges/locations are allowed, but C01 base links remain.
        if not set(map(tuple,expected['edges'])) <= set(map(tuple,package['edges'])):
            raise ValueError('required contract edges missing')
        if package['locations'] != expected['locations']:
            raise ValueError('source locations must match the C01 evidence exactly')
    elif package['grade'] == 'legacy_incomplete':
        # Rebuild from the three registered original files: never trust a forged
        # incomplete package to bypass C01 validation or alter pending status.
        from .legacy_adapter import adapt_legacy
        if package != adapt_legacy(root, [r['path'] for r in inputs]):
            raise ValueError('legacy package differs from deterministic adapter')
    else:
        raise ValueError('unknown grade')
    if not inputs or len({r['path'] for r in inputs}) != len(inputs):
        raise ValueError('unique input artifacts required')
    for row in inputs:
        if artifact(root,row['path'],row['version']) != row:
            raise ValueError('input hash mismatch')
    result = artifact(root,result_path,ADAPTER_VERSION)
    if json.loads(safe_path(Path(root),result_path).read_text()) != package:
        raise ValueError('result JSON differs from package')
    identity = {'inputs':sorted(inputs,key=lambda x:x['path']), 'result':result,
                'package_digest':digest(package),'predecessors':sorted((k[0],k[1],v) for k,v in (predecessors or {}).items())}
    run = digest(identity)
    revisions = {}
    if db.execute('SELECT 1 FROM ana_runs WHERE input_digest=?',(run,)).fetchone():
        return run
    result_id = _artifact(db,result)
    db.execute('INSERT INTO ana_runs(id,input_digest,contract_version,spec_version,analysis_version,adapter_version,grade,result_artifact,diagnostics,bundle) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (run,run,bundle['contract_version'],bundle['spec_version'],bundle['analysis_version'],
                ADAPTER_VERSION,package['grade'],result_id,canonical(package['diagnostics']),canonical(bundle)))
    for row in inputs:
        db.execute('INSERT INTO ana_inputs VALUES(?,?)',(run,_artifact(db,row)))
    for table,kind in zip(TABLES,KINDS):
        for obj in bundle[table]:
            ident = obj['id']; rev = digest(obj)
            key = (kind,ident)
            if key in revisions:
                raise ValueError('duplicate object ID')
            revisions[key] = rev
            db.execute('INSERT INTO ana_objects VALUES(?,?) ON CONFLICT DO NOTHING',key)
            previous = (predecessors or {}).get(key)
            existing = db.execute('SELECT previous_revision FROM ana_revisions WHERE kind=? AND object_id=? AND revision=?',(*key,rev)).fetchone()
            if existing is not None and previous is not None and existing[0] != previous:
                raise ValueError('predecessor conflict')
            db.execute('INSERT INTO ana_revisions VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                       (*key,rev,obj.get('revision'),previous,canonical(obj)))
            db.execute('INSERT INTO ana_run_revisions VALUES(?,?,?,?)',(run,*key,rev))
            if fail_after == len(revisions):
                raise RuntimeError('synthetic import interruption')
    if set(predecessors or {}) - set(revisions):
        raise ValueError('unknown predecessor object')
    for fk,fi,relation,tk,ti in package['edges']:
        db.execute('INSERT INTO ana_edges VALUES(?,?,?,?,?,?,?,?)',
                   (run,fk,fi,revisions[(fk,fi)],relation,tk,ti,revisions[(tk,ti)]))
    for loc in package['locations']:
        raw, hashed = loc.get('raw_path'),loc.get('snapshot_sha256')
        matches = db.execute('SELECT id FROM source_versions WHERE raw_path=? AND sha256=?',(raw,hashed)).fetchall()
        # Multiple upstream identities are ambiguous; leave NULL rather than choose.
        upstream = matches[0][0] if len(matches)==1 else None
        db.execute('INSERT INTO ana_locations VALUES(?,?,?,?,?,?,?)',
                   (run,loc['evidence_id'],'evidence',raw,hashed,upstream,canonical(loc['selector'])))
    for v in bundle['verifications']:
        for i in v.get('issues',[]):
            db.execute('INSERT INTO ana_issues VALUES(?,?,?,?,?,?,?,?)',
                       (run,v['id'],'verification',i['id'],i['description'],int(i['blocking']),i['status'],i.get('resolution')))
    return run


def load_bundle(path, run_id):
    with connection(path) as db:
        row = db.execute('SELECT bundle FROM ana_runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            raise ValueError('unknown run')
        return json.loads(row[0])
