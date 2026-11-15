# experience-mining

**사람이 승인한 범위 밖으로 LLM 에이전트가 자료를 읽거나 결론을 쓰지 못하게 막는 SQLite 기반 작업 큐와 근거 추적 도구**

과거 GitHub PR, 리뷰, ChatGPT 대화에서 "내가 무엇을 발견하고 어떤 판단을 했는지"를 복원하는 작업을 여러 에이전트 세션에 나눠 맡기려고 만들었습니다. 이 레포는 그중 **도구 부분**만 공개합니다. 실제로 수집한 데이터와 분석 결과는 들어 있지 않습니다.

```
Python ≥ 3.9 · 표준 라이브러리만 사용 · 단위 테스트 173개(합성 fixture, 네트워크 없음)
```

## 문제와 해결

**문제**: 에이전트 세션에 "influy 레포 PR을 분석해 줘"라고 맡기면 이런 일이 생깁니다.

- 세션이 중간에 끊기면 다음 세션은 어디까지 했는지 모릅니다. 대화 요약만 믿고 "완료"로 처리하거나, 같은 작업을 두 세션이 동시에 잡습니다.
- 승인은 "PR 목록 수집"까지만 했는데 에이전트가 ChatGPT 대화 본문까지 열어 봅니다.
- 분석 에이전트가 자기가 쓴 결론에 스스로 "검증 완료"를 붙입니다. AI 추론과 기록상 사실이 섞여 노트에 들어갑니다.
- 명세 문서가 바뀌었는데 예전 명세로 만든 결과가 그대로 "유효"로 남습니다.

**해결**: 모든 실행을 큐의 작업 단위로만 허용하고, 아래 조건을 **완료 시점에 DB 트랜잭션 안에서 다시 검사**합니다. 호출자가 미리 했다고 주장하는 검사는 믿지 않습니다.

| 막는 것 | 방법 |
| --- | --- |
| 승인 범위 밖 실행 | 승인을 `projects / operations / roles / phases / input_paths / output_roots / spec_sha256 / 유효기간` 정규형으로 저장하고, 등록·점유·갱신·완료 때마다 다시 대조 (`execution_control.grant`, `check`) |
| 중복 실행·유령 완료 | 15분 임대 토큰으로 점유(`claim`), 만료되거나 소유자가 다르면 완료 거절. 완료 표시는 결과 파일 해시·체크포인트·실제 그래프와 대조 (`audit_completion`) |
| 자기 검증 | `VERIFIED` 전이에는 작성자와 다른 검토자의 독립 PASS가 필요. 분석 역할은 PASS·상태·발행 필드를 쓸 수 없음 |
| 명세 변경 후 낡은 결과 | `reconcile`이 명세·입력 파일 해시 변경을 감지하면 영향받는 후보의 리뷰를 stale로, 발행을 무효로 표시하고 재검증 작업을 승인 대기 상태로 만듦 |
| 반쯤 저장된 결과 | 파일은 temp → fsync → no-replace `link`로 설치하고, DB 결과·큐 완료·체크포인트는 하나의 트랜잭션으로 확정 |

## Quick start

```bash
git clone https://github.com/xseojungx/experience-mining.git
cd experience-mining
python3 -m unittest discover -s tests
```

예상 출력(로컬 macOS, Python 3.9.6에서 확인):

```
.............................................................................
----------------------------------------------------------------------
Ran 173 tests in 2.394s

OK
```

CLI 확인:

```bash
python3 -m tools.work_queue --help
python3 -m tools.chatgpt_pipeline --help
```

테스트는 `tempfile`로 만든 합성 작업 공간에서만 돕니다. 실제 데이터, 네트워크, 원본 저장소를 건드리지 않습니다. 등록부터 발행까지 이어지는 전체 흐름의 합성 예제는 [`tests/test_execution_control.py`](tests/test_execution_control.py)와 [`tests/test_analysis_runtime.py`](tests/test_analysis_runtime.py)에 있습니다.

## 레퍼런스

### `python3 -m tools.work_queue` (작업 큐 CLI)

| 명령 | 인자 | 설명 |
| --- | --- | --- |
| `enqueue` | `approval_id project origin operation unit_key --contract FILE` | 작업 등록. `--contract`는 `{contract, grant_id, dependencies}` JSON. 승인을 새로 만들지 않음 |
| `claim` | – | 실행 가능한 작업 하나를 15분 임대로 점유하고 토큰 출력 |
| `renew` | `id token` | 임대 연장 |
| `finish` | `id token` (stdin: JSON) | 수집 작업 완료. stdin `{"sources":[...], "result":{...}, "next_unit":...}` |
| `analysis-finish` | `id token` (stdin: JSON) | 분석 작업 완료. stdin `{"request":..., "handoff":...}` → `analysis_runtime.complete` |
| `retry` | `id token reason` | 실패 기록. 백오프 후 재시도, 기본 최대 4회 |
| `resume` | `id reason` | `held` 작업을 재개 사유와 함께 다시 활성화 |
| `audit` | – | 읽기 전용. 결과 파일·체크포인트·완료 표시 불일치, 고아 파일 보고 |

### 주요 Python API (`tools/execution_control.py`)

```python
grant(db, ident, approval_id, scope, source, *, valid_from, valid_until)
register(db, scope, key, contract, grant_id, root, *, dependencies=(), max_attempts=4)
check(db, unit, root, *, now=None, dependencies=True)       # 승인·입력·명세 해시 재검사
audit_completion(db, ident, root)                            # 완료 표시 ↔ 실제 산출물 대조
split(db, group, manifest) / merge_split(db, group, unit)    # 큰 PR을 겹침·누락 없이 분할
```

모든 쓰기 API는 호출자가 연 `IMMEDIATE`/`EXCLUSIVE` 트랜잭션과 `PRAGMA foreign_keys=ON`을 요구하고, 스스로 commit하지 않습니다. 계약 필드 정의는 `validate_contract`, 전체 실행 계약은 [`tools/EXECUTION_CONTROL.md`](tools/EXECUTION_CONTROL.md)를 보세요.

### 상태와 역할 (`tools/analysis_contract.py`)

- 후보 단계: `DISCOVERED → INGESTED → MINED → LINKED → INVESTIGATED → VERIFIED → PUBLISHED`. `held`/`failed`는 작업 처분이고 단계가 아닙니다.
- 주장 종류: `recorded_fact` / `ai_inference` / `user_confirmation`. 노트에는 검증된 사실과 사용자 확인만 들어갑니다.
- 논리 역할 7개: `pr_mining`, `review_analysis`, `code_evolution`, `chatgpt_linking`, `behavior_analysis`, `independent_verification`, `note_writing`. 역할 수는 세션 수와 무관합니다.

## 가이드: 한 후보를 발견부터 발행까지

1. **승인 등록**: 사람이 승인한 원문 위치를 `approvals.source`에 연결하고, 정규형 scope로 `grant()`를 만듭니다. 자연어 이력만으로는 grant가 만들어지지 않습니다.
2. **reconcile 후 commit**: 명세·입력 변경을 먼저 stale로 기록합니다. 이후 작업이 거절돼 rollback돼도 이 무효화는 남습니다.
3. **등록**: `register()`에 계약, grant, 선행 작업을 넘깁니다. 같은 계약은 같은 ID, 충돌하는 계약은 오류입니다. 선행 작업으로는 이미 존재하는 작업만 받으므로 순환이 생기지 않습니다.
4. **점유와 실행**: `claim`으로 토큰을 받고, 토큰이 유효한 동안에만 원본을 읽습니다. `file_path`가 읽기·쓰기 경로를 제한합니다(출력은 `results/`, `vault/`만).
5. **완료**: 결과 저장, 도메인 상태 변경, 큐 완료, 체크포인트를 `analysis_runtime.complete` 한 트랜잭션으로 확정합니다.
6. **독립 검증과 발행**: 다른 검토자의 PASS가 현재 revision에 있어야 `VERIFIED`가 되고, 핵심 사실·기여·판단이 모두 확인돼야 템플릿 노트로 발행됩니다.

## ChatGPT 대화 파이프라인 (`tools/chatgpt_pipeline`)

내보낸 ChatGPT 대화를 **제목만으로 먼저 분류**하고, 개발 관련 대화의 코드·오류·경로 단서가 있는 구간만 SQLite FTS5(unicode61 + trigram)로 색인합니다. 이메일, 전화번호, 홈 디렉터리, 토큰 패턴은 가립니다. 네트워크 호출이 없습니다. 자세한 단계와 개인정보 경계는 [`tools/chatgpt_pipeline/README.md`](tools/chatgpt_pipeline/README.md)에 있습니다.

## 설계 결정과 트레이드오프

| 결정 | 고른 이유 | 고르지 않은 대안 |
| --- | --- | --- |
| SQLite + 파일 해시 | 단일 머신·단일 사용자 작업이라 서버가 필요 없음. 트랜잭션과 `BEGIN EXCLUSIVE`로 writer 직렬화가 공짜로 됨 | Postgres·Redis 큐: 운영 부담 대비 이득 없음 |
| 완료 시점 재검사 | 에이전트가 "검사했다"고 말하는 것을 믿지 않기 위해 | 등록 시 한 번만 검사: 실행 중 승인 철회·명세 변경을 놓침 |
| 만료된 작업을 자동 재실행하지 않고 `failed` 처분 | 승인 범위가 바뀌었을 수 있는 작업을 조용히 다시 돌리지 않기 위해 | 자동 재시도: 편하지만 무승인 실행 위험 |
| 승인을 정규형 JSON으로만 인정 | 자연어 승인("PR 좀 봐 줘")은 범위 해석이 사람마다 다름 | 자연어 이력 그대로 사용: 범위 확장 사고의 원인 |
| 표준 라이브러리만 사용 | 수집 대상 자료가 개인 데이터라 의존성 공급망을 최소화 | ORM·CLI 프레임워크 |
| 린트는 오류 계열(`E9`, `F`, `B`, `I`)만 강제 | 기존 코드가 한 줄에 여러 문장을 쓰는 밀도 높은 스타일이라, 포맷 규칙을 강제하면 diff가 전체 재작성이 됨 | `ruff format` 전면 적용 |

## 개발 가이드

```
tools/
  execution_control.py   승인 grant, 계약 검증, 의존성, 분할·병합, 완료 감사
  work_queue.py          큐 CLI (enqueue/claim/renew/finish/retry/resume/audit)
  analysis_contract.py   후보 단계, 주장 종류, 평가 배점, 번들 검증
  analysis_store.py      분석 결과 저장소(그래프·revision), 마이그레이션
  analysis_runtime.py    분석·독립 검증·전이·발행, 원자적 파일 설치, 무효화 전파
  audit_state.py         상태·체크포인트 정합성 감사
  collect_*.py, recover_*.py   GitHub PR 수집기 (큐 토큰 없이는 실행 안 됨)
  migrate_*.py           운영 DB 마이그레이션 (백업·무결성·복원)
  chatgpt_pipeline/      ChatGPT 내보내기 분류·검색
  jev_*.py               외부 분류 API로 대화를 프로젝트·카테고리별로 나누는 일회성 실험 스크립트
tests/                   unittest, 합성 fixture만 사용
state/schema.sql, state/migrations/   DB 스키마 (실제 DB는 공개하지 않음)
spec/MASTER_PLAN.md, spec/NOTE_TEMPLATES.md, AGENTS.md
                         도구가 해시로 묶는 명세 3종. 바뀌면 관련 결과가 stale 처리됨
```

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .                                  # 린트
mypy                                          # 타입 체크 (tools/)
python3 -m unittest discover -s tests -v      # 테스트
```

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): push·PR마다 Python 3.9와 3.12에서 ruff → mypy → unittest를 돌립니다. 막는 것: 문법 오류, 정의되지 않은 이름, 사용하지 않는 import, 예외 체이닝 누락 같은 bugbear 패턴, 타입 오류, 승인·임대·트랜잭션 규칙의 회귀.

**LLM 에이전트용 지침**: [`AGENTS.md`](AGENTS.md)에 자료 권한, 승인 대조 절차, 인계 규칙이 있습니다.

## 공개 범위

이 레포에는 도구와 스키마, 합성 테스트만 있습니다. 다음은 `.gitignore`로 제외했습니다.

- 수집한 PR·리뷰 원본, ChatGPT 내보내기(`data/`), 운영 DB와 상태 파일(`state/*`, 스키마 제외)
- 분석 결과·체크포인트·명세 개정 승인 기록(`results/`, `checkpoints/`, `spec/revisions/`)
- API 키가 든 `.env`. `tools/jev_*.py`는 공개하지만, 이 스크립트가 쓰는 키와 결과는 `results/` 아래에 있어 함께 제외됩니다.

## 한계와 로드맵

- **mypy는 기본 수준입니다.** 대부분 함수에 타입 힌트가 없고 `check_untyped_defs`를 끈 상태입니다. 스크립트 실행용 fallback import(`except ImportError: from state import ...`)가 있는 모듈 몇 개는 설정에서 일부 오류 코드를 끄거나 검사에서 뺐습니다(`pyproject.toml` 참고).
- **스크립트 직접 실행과 패키지 실행이 섞여 있습니다.** `python3 -m tools.x`가 기준이고, 일부 오래된 수집 스크립트는 `python3 tools/x.py`로만 동작합니다.
- **승인 발언의 진위는 증명하지 않습니다.** 승인을 등록하는 사람이 실제 발언을 확인해야 합니다.
- **문서와 주석이 주로 한국어**이고, 코드 스타일이 촘촘합니다.
- **실행 단위는 후보 1개입니다.** 여러 후보를 묶은 실행은 먼저 후보별 계약으로 나눠야 합니다.
- 파일 감시 데몬은 없습니다. 무효화는 `reconcile` 호출 시점에만 감지합니다.

## License

[MIT](LICENSE)
