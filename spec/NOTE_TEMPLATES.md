# Obsidian 노트 템플릿

출처는 「프로젝트 경험 추출」의 사용자 메시지 `d1b2062f-7b2a-4bab-8e54-45db9bf33fc9`이다. 아래 두 블록은 파일명 규칙과 출력 지시까지 포함한 원문이다. 예시의 `verified: false`와 하단 규칙의 `true`를 모두 보존했다. 정식 experience 노트를 발행할 때는 검증을 마친 후보에 하단 규칙인 `verified: true`를 적용한다. 확인 대기 후보는 별도 저장한다.

경험 아카이브에 해당되는 곳: log
```text

# 유형 판단
내가 유형을 말하지 않으면 아래 기준으로 고른다.
- decision: 둘 이상의 선택지를 비교하고 하나를 골랐다. 발단이 버그였어도 여기에 쓴다.
- problem: 고장이 있었고, 원인을 찾으면 해결이 거의 정해진다. 해결책이 둘 이상이었고 그중에서 골랐다면 decision이다.
- qna: 내가 질문하고 답을 받았다.
- doc: 계속 갱신하는 프로젝트 문서. 현재 상태만 적는다.

# 출력 형식
1. 파일명 제안 한 줄
2. 노트 전체를 마크다운 코드 블록 하나에 담는다. 아래 틀을 그대로 쓴다.
3. 노트 아래에 "확인 필요 목록"을 붙인다.

## decision — 파일명 `ADR-<번호> - 제목.md`

~~~markdown
---
type: decision
adr: <번호. 모르면 비움>
project: <프로젝트명>
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
topics: []
status: accepted
tags: []
---
# ADR-<번호> — <제목>

## 발단 (증상·원인)
> 버그나 문제에서 시작된 결정일 때만 쓴다. 아니면 이 섹션을 지운다.

## 맥락
- 목표:
- 제약:

## 고려한 옵션
| 옵션 | 장점 | 단점 |
| --- | --- | --- |
|  |  |  |

## 사고 과정

## 결정과 이유
> **결정:** 

## 결과 · 트레이드오프

## 잘못 알고 있던 것
- (있으면 한 줄. 없으면 "없음")

## 관련 노트
- [[ ]]
~~~

## problem — 파일명 `YYYY-MM-DD - 제목.md`

~~~markdown
---
type: problem
project: <프로젝트명>
created: <YYYY-MM-DD>
topics: []
status: resolved
tags: []
---
# <YYYY-MM-DD> — <제목>

## 증상

## 원인

## 해결
(실행한 명령이나 코드가 있으면 코드 블록으로)

## 재발 방지

## 잘못 알고 있던 것
- (있으면 한 줄. 없으면 "없음")

## 관련 노트
- [[ ]]
~~~

## qna — 파일명 `MMDD 질문 요약.md`

~~~markdown
---
type: qna
project: <프로젝트명>
with: <질문한 대상. 예: ChatGPT, 멘토 이름>
created: <YYYY-MM-DD>
topics: []
status: answered
tags: []
---
# Q&A — <제목>

## 배경 · 왜 물었나

## 질문
> 

## 답

## 내 이해 (내 말로)
(내가 정리해서 말한 내용만 쓴다. 없으면 비워 둔다)

## 후속
- [ ] 

## 관련 노트
- [[ ]]
~~~

## doc — 파일명 `문서 이름.md`

~~~markdown
---
type: doc
project: <프로젝트명>
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
topics: []
---
# <문서 이름>

## 개요

## 내용

## 관련 결정
- [[ ]]
~~~

# frontmatter 규칙
- `topics`는 기술 주제를 소문자 영어로 1~5개 쓴다. 예: `[redis, rate-limit, postgres]`
- `tags`는 아래 목록에서만 고른다. 해당 없으면 빈 배열로 둔다.
  `interview`, `til`, `misconception`, `evergreen`, `study/later`, `question/open`
- `status`는 유형별 기본값(decision `accepted`, problem `resolved`, qna `answered`)을 쓴다. 아직 해결되지 않았으면 내게 묻는다.
- `관련 노트`의 `[[ ]]`는 내가 알려 준 노트 이름만 채운다. 모르면 그대로 둔다.

이제 아래 내용을 정리해 줘.

```



취업 소재 라이브러리에 해당되는 곳: experience
````
# 출력 형식
정리 단계에서는 아래 순서로 출력한다.

1. 파일명 제안 한 줄: `제목 (상황 한 줄).md`
2. 노트 전체를 마크다운 코드 블록 하나에 담는다. 아래 틀을 그대로 쓴다.

```markdown
---
type: case
project: <프로젝트명>
created: <오늘 날짜 YYYY-MM-DD>
updated: <오늘 날짜 YYYY-MM-DD>
competencies: []
question_types: []
metric: <수치 1개와 측정 조건. 없으면 비움>
verified: false
used_in: []
status: draft
---
# <제목>

## 한 줄 요약
> <상황, 판단, 결과가 한 문장에 들어가게>

## 상황
- 목표:
- 제약:
- 내 역할:

## 판단
> 가장 길게 쓴다. 무엇을 보고, 무엇을 버리고, 왜 이걸 골랐나.

## 결과
- 수치: (측정 조건 포함)
-

## 근거
- (내가 말한 근거 자료: 커밋, 문서, 로그, 이슈 등. 없으면 "없음")

## 30초 버전
> 면접에서 말로 설명하는 분량. 상황 → 판단 → 결과 순서.

## 예상 꼬리 질문
- **Q.**
  - **A.** (내가 답한 내용만. 답을 못 한 질문은 A를 비워 둔다)
```


# frontmatter 규칙
- `competencies`는 아래 고정 어휘에서만 1~3개 고른다.
  성능 개선, 근본 원인 분석, 설계·트레이드오프, 협업·소통, AI 활용·통제, 안정성·장애 대응, 오너십, 학습 속도, 사용자 관점
- `question_types`는 아래 고정 어휘에서만 1~3개 고른다.
  가장 어려웠던 문제, 갈등·의견 충돌, 실패·실수, 주도적 개선, 빠른 학습
- `verified`는 항상 `true`, `status`는 항상 `draft`, `used_in`은 항상 빈 배열로 둔다.
````
