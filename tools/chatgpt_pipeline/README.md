# ChatGPT local evidence pipeline · chatgpt-c-v1

이 모듈은 승인된 로컬 내보내기에 대한 제목 선분류·메시지 구간 검색·GitHub 연결 후보를 준비한다. 공통 분석 계약/큐/독립 검증/발행 엔진을 대체하지 않는다. 네트워크 호출이 없고 운영 DB 쓰기 경로도 없다.

실제 실행 승인과 산출물은 `results/chatgpt-c-2026-11-15/AUTHORIZATION.json`, `PRE_IMPLEMENTATION.md` 및 새 체크포인트를 참조한다. 기본 실행 디렉터리는 그 전용 results 경로다. 명세 세 파일 해시와 명시 승인 파일을 검사한다. 생성 DB 이름은 catalog.sqlite3/search.sqlite3으로 제한하며 쓰기는 `results/chatgpt-c-*` 또는 합성 테스트용 system-temp 안에서만 가능하다. 심볼릭 링크/경로 탈출을 거절한다. output 디렉터리의 nonblocking flock으로 동시에 두 실행이 쓰는 것을 거절한다. 운영 DB 사본은 별도 읽기 전용 연결로만 사용한다.

## 실행

저장소 루트에서 실행한다. 명령은 승인된 실제 처리이며 프로젝트 앱 실행/벤치마크가 아니다.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline titles
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline extract
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline github
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline link
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline export
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline validate
PYTHONDONTWRITEBYTECODE=1 python3 -m tools.chatgpt_pipeline summary
```

`run`은 위 변경 단계를 순서대로 수행한다. 다른 변경 명령이 실행 중이면 잠금 해제 후 재시도한다. `search`, `trace`, `summary`는 read-only다. 테스트는 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_chatgpt_pipeline -v`.

```sh
python3 -m tools.chatgpt_pipeline search 'BottomSheet' --limit 5
python3 -m tools.chatgpt_pipeline search '드래그' --substring --limit 5
python3 -m tools.chatgpt_pipeline search 'useQuery' --since 2025-04 --until 2025-10
python3 -m tools.chatgpt_pipeline search --pr 37 --repository INFLUY/Influy-FE --limit 5
python3 -m tools.chatgpt_pipeline trace SEGMENT_ID
```

검색은 입력을 literal phrase로 escape한다. unicode61 FTS5는 식별자/파일 경로/오류 구문을, trigram FTS5는 3자 이상 한글 부분 문자열을 지원한다. `--account`, `--conversation-id`, `--message-id`, `--since`, `--until`, `--project`를 결합할 수 있다. project는 본문 문자열에 기반한 **미확정 단서** 필터이며 확정 귀속이 아니다. 기본 검색은 기간·프로젝트로 제한하지 않는다. 기간 상한은 문자열 비교를 하므로 하루 전체를 지정하려면 UTC ISO 시각을 명시하거나 다음 날 경계를 사용한다. PR 번호는 본문 속 `PR 37`·`#37` 등 실제 표기에 맞춰 검색하고 저장소/파일 단서와 대조한다. 순위는 관련성 단서이며 기여도 점수/사실 검증이 아니다.

## 단계와 개인정보 경계

1. 원본 파일의 상대 경로/크기/SHA-256을 등록한다. 원본을 쓰거나 이동하지 않는다. 백업 ZIP은 열지 않는다. 첨부/기타 파일은 bytes 해시만 계산하고 파싱하지 않는다. account ID는 물리 출처 등록 키다. 소유자/논리 A/B를 폴더명에서 추정하지 않는다. export batch는 계정 파일 목록과 해시의 결정적 지문이다.
2. 모든 분할 JSON을 구조적으로 decode하되 대화의 id/title 필드만 제목 분류에 사용한다. summary/sectioned/settings/index.html 내용은 사용하지 않는다. title-v1은 명확한 개발 문자열을 DEV, 아주 좁은 명확한 일상 주제만 NON_DEV, 나머지와 충돌을 AMBIGUOUS로 분류한다. 프로젝트·기술어 부재는 제외 사유가 아니다. catalog에 제목/규칙/사유/처리/재검토 상태와 원본 위치를 저장한다. NON_DEV 행에는 본문·메시지·내용 개정 지문을 저장하지 않는다.
3. 제목 패스가 먼저 저장된 성공 파일에 한해 DEV/AMBIGUOUS mapping/content를 접근한다. NON_DEV는 그 전에 continue한다. JSON 전체 문법 파싱은 의미적 본문 열람/분석과 구별한다. segment-v1은 단락/코드펜스 중 명확한 개발/코드 단서가 있는 구간만 검색 대상으로 선별한다. 무관·불명확 구간은 복사/색인하지 않고 held/empty 상태만 저장한다. 개발 대화에도 무관한 내용이 섞일 수 있어 전체 메시지를 검색용으로 복사하지 않는다. 이메일/전화번호/홈 디렉터리 계정/명백한 토큰·비밀번호 패턴을 가린다. 이 규칙은 포괄적 익명화나 의미 정확도를 보증하지 않는다.
4. 원본의 모든 분기 노드를 보존한다. 노드의 parent와 원래 children, parent에서 계산한 children을 각각 둔다. 메시지 작성 역할/시각/내용 지문을 보존하고 유효하지 않은 첨부는 내용을 열지 않는다. 문자열이 아닌 멀티모달 part는 색인하지 않는다. 메시지 role이 아닌 실제 기여 주체는 별도 확인 대상이다.
5. assistant는 ai_proposal_unverified, user는 user_statement_unverified 또는 명시적 수행 표현을 발견한 user_reported_action_unverified로 표시한다. 마지막 값도 실제 채택·구현 확인이 아니다. GitHub와의 문자열 연결도 확인된 구현·성과·기여를 뜻하지 않는다. 이 실행은 confirmation claim, 평가, 검증 PASS, VERIFIED/PUBLISHED를 만들지 않는다.

## 식별·재처리·실패 복구

계정·대화·메시지는 provider/source_account_id/conversation_id/message_id/revision을 갖는다. 실제 저장 키는 그 배열의 SHA-256이며 conversation revision과 mapping node도 포함해 동일 메시지 ID의 분기/다른 snapshot을 잃지 않는다. 개정 지문은 canonical JSON의 내용 주소이며 역사적 개정 순서를 추정하지 않는다. 원 ID가 없으면 파일 지문+JSON Pointer 또는 mapping node를 이용한 surrogate임을 명시한다. 분할 파일 순서·제목은 고유 ID로 쓰지 않는다.

위치는 export batch, 파일 상대 경로·SHA-256, conversation JSON Pointer, message mapping pointer, content part pointer, 원래 문자열의 Unicode character start/end다. redacted text 지문과 원본 content 지문을 구별한다. 전체 메시지 원문을 DB에 복사하지 않아도 trace로 승인된 해당 span을 재조회하고 마스킹 후 byte 동등성을 확인한다. Python 문자열 오프셋은 UTF-8 바이트 오프셋이 아니다.

파일 단위 완료와 파생 DB 쓰기/catalog 상태는 ATTACH된 두 DELETE-journal DB의 같은 transaction으로 확정한다. 중단 파일은 rollback하고 실패 종류만 기록한다(원문이 들어갈 수 있는 예외 문자열을 기록하지 않는다). 성공한 다른 파일은 보존하며 재실행하면 실패 파일만 다시 처리한다. 입력 해시가 바뀌면 이전 등록으로 처리하지 못한다. 동일 입력은 stages/UNIQUE로 중복 방지한다. 새 batch·snapshot은 이전 원본 위치/개정을 보존한다. 계정 간 동일 해시도 원본을 합치지 않고 candidate relation만 둔다. 비슷한 제목을 동일 대화로 판정하지 않는다.

코드/분류 규칙을 개정할 때는 새 rule/version과 새 output 디렉터리로 다시 빌드한 뒤 제외/재검토 영향을 대조한다. 기존 분류를 조용히 덮어쓰거나 NON_DEV 본문부터 검색하지 않는다. CLI는 원본 쓰기·운영 import·승격 명령을 제공하지 않는다. 복구는 이 전용 파생 디렉터리만 새로 만들며 원본/운영/B 산출물을 원복하지 않는다.

## GitHub 양방향 검색

github-operational-snapshot.sqlite3은 운영 DB의 mode=ro/query_only 연결에서 backup API로 만든 일관된 별도 사본이다. 사본조차 본 처리에서는 수정하지 않는다. source_versions에서 지정한 GitHub 로컬 파일만 SHA-256 대조 후 재사용한다. detail/reviews/comments/issue_comments/commits/files의 page 배열과 JSON Pointer를 보존한다. 파일 patch 부재/기존 수집 누락은 새로 수집하거나 복원하지 않는다. 원본 id/커밋 SHA/작성자/시각/PR/원본 지문을 유지한다.

각 GitHub 문서→ChatGPT segment, 각 segment→GitHub 문서에서 파일/함수/컴포넌트/오류/URL 단서를 추출하고 실제 문자열 포함 여부를 재확인한다. 상위 6개 단서, 단서당 상위 8개, source당 연결 최대 8개로 제한한다. 흔한 API 식별자는 제외하고 프로젝트명만 있으면 insufficient로 둔다. 전체 대조나 의미 검색 완료를 주장하지 않는다. search_runs는 양 방향의 검색 단서/범위/코퍼스 지문/상한/결과 없음/미검색을 남긴다. confirmed/candidate/insufficient/conflicting/not_found/not_searched 상태를 스키마에서 지원한다. 자동 confirmed/conflicting 판정은 하지 않는다. 연결의 양쪽 해시/식별자는 view/join과 LINK_CANDIDATES.jsonl에서 확인한다. FOR_CODEX_B는 PR37/70/45 범위 연결만 별도로 제공하며 후보별 주장 관련성은 미검증이다.

## C01~C04 호환 및 실제 통합 보류

추가 실제 확인: 공통 C01 감사/C02 artifact의 safe_path도 경로 구성요소에 chatgpt가 있으면 거절한다. 따라서 전용 results 경로의 package 자체도 현재 공통 저장 API에 바로 투입할 수 없다. CONTRACT_BOUNDARY_RESULT.json에서 세 경계가 그대로 차단함을 읽기 전용 호출로 확인했다. 이번의 호환은 C01 데이터 검증 및 기존 C02 package builder 수준이다. 중립 이름의 staging 폴더로 옮겨 경계를 우회하는 처리도 하지 않았다. 실제 저장·실행 경로 통합은 승인된 선택적 원본 resolver와 경로 검사 확장 이후에 가능하다.

`--pr`/`--repository` 검색은 현재 코퍼스의 연결 후보에 기반한 필터다. 해당 PR과의 관계가 확정되었다는 뜻이 아니며, 아직 연결 후보가 없는 메시지는 이 필터에서 나오지 않는다. 텍스트 단서 검색과 병행한다.

`C01_EVIDENCE_BUNDLE.json`은 기존 `validate_bundle`을 통과한 evidence-only bundle이다. `C02_COMPATIBILITY_PACKAGE.json`은 기존 `analysis_store.contract_package`로 생성한다. 필수 작성 시각 등 부족한 근거는 PENDING_EVIDENCE에 남기고 임의로 채우지 않는다. 근거 source_id는 provider/account/conversation/message/revision의 canonical JSON 문자열이고 raw_path/sha256/location/version을 공통 필드에 연결한다. 추가 provenance와 메시지 역할은 확장 필드로 보존한다. 이 계약 검사는 의미·기여·계정 소유의 검증이 아니다.

현재 C03/C04 입력 경로 검사기는 chatgpt 경로를 명시적으로 차단한다. 그 차단을 제거하지 않았으므로 이 package를 운영 분석 작업에 즉시 사용하지 않는다. 운영 통합은 별도 승인된 변경안에서 허용된 대화/메시지/span만 접근하는 전용 source resolver를 설계하고, 원본 등록/입력 hash scope·분류 gate·내용 변경 영향을 공통 grant/queue/checkpoint에 연결해야 한다. 현재 구현에서 raw 파일 전체를 일반 공통 입력으로 allowlist하는 방법은 NON_DEV 경계를 약화하므로 적용하지 않는다.

통합 시에는 승인된 source_versions 등록 및 source FK 연결 → 허용된 근거만 후보별 새 C01 revision에 연결 → C02 package 저장 → C03 정확한 승인/grant/작업 계약 등록 → C04 evidence_link 영향 사건·새 독립 검토 순서를 따른다. 이번 package에 candidate/claim/review/note는 비어 있고 기존 후보 개정/검증을 재사용하지 않는다. CHANGE_IMPACT_PROPOSAL은 pending_approval 제안이며 ctl_changes/ctl_revalidation을 수정하지 않는다. 운영 DB·기존 후보·B 보고서·Obsidian 발행은 전부 그대로다.
