# C03+C04 실행 계약 · c03-c04-v1

기존 Python `queue_units`·SQLite·C01 계약·C02 저장소를 확장한다. 수집 상태 pending/running/complete와 후보 DISCOVERED→INGESTED→MINED→LINKED→INVESTIGATED→VERIFIED→PUBLISHED를 구분한다. held/failed는 추가 job 처분이며 새 분석 단계가 아니다. 7논리역할·L0~L3·5단계를 바꾸지 않는다.

## 경계와 진입점

- `execution_control.py`: 추가 스키마/승인/작업 계약·의존성·전달 객체·분할/병합·실제 완료 감사.
- `work_queue.py`: 등록/점유/갱신/실패/재개/수집 완료. `analysis-finish`는 `analysis_runtime.complete`로만 분석을 완료한다.
- `analysis_runtime.py`: C02 현재 head 등록·분석·독립 검증·전이·변경·발행, 원자적 파일 저장, 고아/손상 감사, 변경 영향·재검증.
- `migrate_control.py`: 운영 DB에 제어 스키마만 추가. C02의 백업·무결성·트랜잭션·복원·durable 기록 재사용. `ana_migrations`는 version2 그대로이고 `ctl_migrations`의 제어 버전은1이다.

모든 쓰기 API는 호출자가 소유한 IMMEDIATE/EXCLUSIVE transaction 및 FK ON을 요구한다. 오류를 잡고 부분 변경을 commit하지 않는다. `analysis_store.transaction`을 사용하면 오류시 rollback한다. API는 commit하지 않는다. 기존 C02의 낮은 수준 import는 역사적 저장이며 현재 head/PASS/발행 권한을 생성하지 않는다. C02 import를 바탕으로 한 `enroll`도 큐 토큰과 명시 승인이 필요하다. 현재 구현은 후보 한 개를 실행 단위로 다루며, 다중 후보 bundle은 먼저 승인된 범위 안에서 후보별 계약으로 나누어야 한다. 기존 다중 후보 legacy run은 그대로 보존한다.

## 승인

기존 `approvals`의 자연어 이력·queue_scopes 존재만으로 새 실행을 허용하지 않는다. 과거 자연어 승인으로 `grant()`를 만들면 거절한다. 미래 실제 실행 승인은 원문 범위를 검토한 뒤 **새 approval ID**에 아래 정규형을 기록하고, 원문 위치를 approvals.source에 연결한다. 기존 이력을 정규형으로 고쳐 쓰지 않는다.

```json
{"kind":"execution_authorization","scope":{"projects":["..."],"origins":["..."],"operations":["..."],"roles":["..."],"phases":["..."],"targets":["..."],"input_paths":["..."],"output_roots":["..."],"decision_ids":["..."],"spec_sha256":{"AGENTS.md":"...","spec/MASTER_PLAN.md":"...","spec/NOTE_TEMPLATES.md":"..."},"analysis_versions":["..."],"states":["..."]},"valid_from":0,"valid_until":1}
```

위 예시는 구조 안내이며 실행 승인이 아니다. `grant`는 scope·유효기간·원문 위치가 이 기록과 정확히 같아야 하며 승인 행 자체의 지문도 묶는다. 이후 등록/점유/갱신/완료마다 유효기간·철회·승인 변경·프로젝트/저장소/작업/논리역할/단계/후보/상태·입출력·분석 버전·결정·명세 해시를 다시 검사한다. 이 시스템은 입력된 승인 발언의 진위를 스스로 입증하지 않는다. 승인 등록자는 실제 사용자 발언을 확인해야 한다. 이번 운영 적용에서는 grant·실행 approval·작업을 만들지 않았다.

작업의 `contract` 필드는 `validate_contract`에 정의되어 있다. 분석 대상은 C01 `revision_digest`로 고정하며, 근거는 원본 경로+SHA-256+등록 source_version ID로 묶는다. 실제 파일을 읽을 수 있는 경계는 `file_path`가 제한한다. ChatGPT 자료는 이 공통 실행기에 허용되지 않는다. 출력은 results/ 및 새 vault/만 허용한다. 수집기의 동적 스냅샷은 별도로 승인된 저장소별 data/raw/github 또는 data/raw/git 루트가 있어야 하며 토큰·등록 scope·실제 PR route를 검사한다.

논리역할: pr_mining / review_analysis / code_evolution / chatgpt_linking / behavior_analysis / independent_verification / note_writing. 이 이름은 세션 수를 뜻하지 않는다. chatgpt_linking 역할 이름이 있다고 본문 읽기 권한이 생기지 않는다.

## 실행·재개·전달

1. 트랜잭션에서 `reconcile(db, root)`를 실행하고 **별도로 commit**한다. 이전 승인 입력·명세/템플릿 변경과 파일 누락을 stale 및 재검증 요구로 기록한다. 이후 거절되는 작업의 rollback이 이 무효화를 취소해서는 안 된다. 일반 큐 CLI는 이 순서를 자동으로 지킨다.
2. `register`/`enqueue`에 정확한 contract, grant_id, predecessor IDs를 전달한다. 동일 계약은 같은 ID, 충돌은 오류다. 새 작업이 기존 작업만 선행자로 받으므로 등록 API를 통해 의존 순환을 만들 수 없다. 선행 작업은 완료 표시·체크포인트·파일 해시·실제 C02 그래프까지 통과해야 한다.
3. 짧은 transaction에서 `claim` 후 commit한다. 모든 writer의 점유가 SQLite 잠금으로 직렬화된다. 실제 원본 읽기/분석은 토큰과 임대가 유효한 동안만 수행한다. 긴 작업은 `renew`한다. 상한에 도달한 만료 작업은 failed 처분이며 자동 재실행하지 않는다.
4. 결과 저장/도메인 변경/큐 완료/체크포인트는 `finish` 또는 `analysis_runtime.complete`와 **한 DB transaction**에서 확정한다. 완료 함수 자체에서 DB의 최신 토큰·임대·승인·입력·대상 개정을 확인한다. 외부 호출자의 사전 검사에 의존하지 않는다.
5. 실패하면 partial transaction을 rollback하고 새 transaction에서 `record_failure`한다. 유효한 점유는 backoff와 최대4회 기본 정책으로 반환한다. 만료/철회/다른 worker 소유면 실패 원인만 기록하고 소유를 빼앗지 않는다. held는 재개 조건과 해결 이유 후 resume; 영구 실패는 정책을 검토한 새로운 명시 작업이 필요하다.

전달 객체 필수: project/candidate_id/target_revision/observations/hypotheses/evidence_ids/unknowns/next_tasks. 배열은 관찰·가설·미확인을 분리한다. 전달 객체는 완료 체크포인트에 저장한다. 큰 PR은 `split`에 기능/파일/스레드 전체 universe·파트별 실제 작업 ID·미검토 범위를 등록한다. 겹침·누락·중복을 검사하고, `merge_split`은 미검토가 없고 병합 작업이 모든 파트에 의존할 때만 확정한다. 파트 완료도 일반 dependency 감사로 확인한다.

CLI: `python3 -m tools.work_queue --help`. 기존 enqueue/claim/finish/retry와 renew/resume/analysis-finish/audit를 지원한다. enqueue의 `--contract`는 `{contract,grant_id,dependencies}` JSON이다. analysis-finish stdin은 `{request,handoff}`다. 승인 발급은 CLI가 자동 수행하지 않는다. 테스트 fixture에 등록부터 발행까지 완전한 합성 예제가 있다.

## 검증·전이·변경

`complete`의 action은 enroll/analyze/review/transition/change/publish이며 role·phase와 실제 허용 상태를 검사한다. 분석자는 내용만 고칠 수 있고 PASS/상태/발행을 끼워 넣을 수 없다. VERIFIED 전이는 별도 저장된 현재 revision의 독립 execution PASS가 있어야 한다. 선택적 INVESTIGATED 생략과 재검증 역전이는 C01의 reason 규칙을 따른다.

독립 검증은 모든 주장·원본 참조·입력 ledger·검토 범위·이유·검토자·개별 판단·기여/채택/대안/후속 변경·반대 근거와 쟁점을 기록한다. PASS/NEEDS_EVIDENCE/CONTRADICTED/PENDING/HELD를 저장하며 C01 status와 대응한다. 반대 근거는 실제 claim/evidence IDs에 연결하고 C02 refutes 간선도 남긴다. 검토자 ID는 후보·주장·평가 작성자와 달라야 한다. 사람/에이전트의 별칭 동일성, 실제 원문 이해, 의미상 진실은 메타데이터로 증명하지 않는다.

PASS라도 핵심 사실/기여/판단 미확인, 부분 coverage, 미답변 핵심 질문, 차단 쟁점, 예전 개정·해시는 승격/발행을 막는다. 실패 후 재검증은 새 review ID와 명시 supersedes 및 쟁점별 원본 근거가 있는 resolution을 쓴다. 이전 개정·원 판정·원 쟁점은 덮어쓰지 않는다.

`invalidate`는 원본/주장/후보/사용자 정정/근거 연결/분석 규칙/명세/평가 기준/검증 기준/템플릿 사건을 지원한다. C02 현재 그래프 역방향으로 영향 후보를 찾고 리뷰 stale, 현재 publication valid=0, 재검증 intake를 기록한다. 새 source_versions 등록은 SQL trigger로도 자동 전파한다. `reconcile`은 승인된 파일의 변경/손상도 검출한다. 규칙 변경은 적용자가 변경 ID와 영향 nodes/candidates를 명시하여 invalidate해야 한다. 파일 감시 daemon은 없다.

재검증 intake는 pending_approval이므로 실행 권한을 주지 않는다. 이미 명시 승인된 독립 검증 작업은 attach_revalidation으로 연결할 수 있고, 현재 입력 전체를 검토한 PASS로 해결할 때 수행 unit ID와 이전 배정 이력을 기록한다. 기존 PASS를 다른 개정에 붙여 쓰지 않는다. 새 원본 버전이 있으면 그 최신 버전도 현재 근거에 포함해야 검증·발행한다. 과거 원본도 함께 보존·대조할 수 있다.

## 발행·파일/DB 복구

experience 발행은 current VERIFIED·독립 PASS·모든 입력/원본·미해결 쟁점/질문·템플릿 해시·중복을 검사한다. renderer는 검증된 fact/confirmation claim IDs를 지정한 sections만 사용한다. 미검증 inference와 평가 객체를 노트에 넣지 않는다. frontmatter는 템플릿의 고정 키/어휘·verified:true/status:draft/used_in:[]를 생성하며, 템플릿의 필수 섹션을 출력한다. 원본 형식 파일은 수정하지 않는다. 출력 파일은 템플릿의 markdown 블록 내부이고, 경로가 파일명 제안에 해당한다. 실제 사용자 경험 발행은 이번 실행 범위에서 제외했다.

파일은 temp+fsync+원자적 no-replace 설치 후 해시를 확인한다. 다른 bytes의 기존 경로는 거절한다. C02 결과 package·관계·현재 head·review/publication·큐 완료·체크포인트는 하나의 DB transaction이다. DB가 rollback돼도 이미 설치한 파일은 보존되는 고아다. 다음 재시도에서 동일 bytes만 재사용하고 모두 검증한 후 등록한다. `audit_files` 및 CLI audit가 고아/손상/누락을 보고하며 자동 성공이나 파일 삭제를 하지 않는다. 이전 publication 파일은 수정하지 않고 새 경로·새 ID로 재발행한다. 외부 사용자가 DB 확인 없이 orphan markdown 파일을 읽는 경우까지 DB atomicity가 보장하지는 않는다. 공식 현재 발행 여부는 ctl_publications.valid·현재 head·유효한 review로 판단한다.

운영 스키마 복구: 이번 체크포인트의 RESTORE.md를 따른다. 이후 새로운 정당한 데이터가 생겼다면 과거 백업을 무조건 복원하지 않는다.
