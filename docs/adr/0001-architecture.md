# ADR-0001 — 구조: 증분 수집기 + SQLite + 결정론적 분석 + 로컬 웹

- 상태: 승인 (2026-10-07, 지휘부)
- 관련: `docs/PLAN.md` §2~§4, `docs/reports/0001-m0.md`

## 맥락

M0 실측(2026-10-07, Claude Code 2.1.215~2.1.291 로그, 스크립트 집계로 숫자만 확인):

- 로그 위치는 `~/.claude/projects/<인코딩된 cwd>/` 아래 세 가지다.
  - 메인 세션: `<세션uuid>.jsonl` (38개)
  - 서브에이전트: `<세션uuid>/subagents/agent-<id>.jsonl` (1,424개, 모든 줄이 `isSidechain=true`, `agentId` 있음)
  - 워크플로: `<세션uuid>/subagents/workflows/wf_<id>/agent-<id>.jsonl`, 그리고 `journal.jsonl`(`started`/`result`만 있고 usage 없음)
- headless(`claude -p`/SDK) 세션은 따로 모이지 않는다. 메인 세션 파일에 `entrypoint="sdk-cli"`로 들어간다(549줄). 대화형 세션은 `"cli"`.
- 줄 종류는 24가지다. 사용량은 `type="assistant"` 줄의 `message.usage`에만 있다. 포함 필드: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.{ephemeral_5m,ephemeral_1h}_input_tokens`, `server_tool_use.{web_search,web_fetch}_requests`, `output_tokens_details.thinking_tokens`, `iterations[]`(일부), `speed`, `service_tier`.
- **한 API 응답이 여러 줄로 기록된다.** 내용 블록마다 한 줄씩이다. 고유 `message.id`는 78,890개이고, 그중 56,547개가 2~6줄에 걸쳐 있다. 같은 id에서 input/cache 값은 같지만 **`output_tokens`는 줄마다 다를 수 있다**(21,348건, 뒤 줄이 더 큼). 또 같은 id가 **서로 다른 파일**에 나오는 경우가 621건 있다(재개·포크로 이력이 복사된 경우).
- 도구 결과는 `type="user"` 줄의 `tool_result` 블록에 있다(86,940건). `content`는 str이거나 `text`/`image`/`document`/`tool_reference` 블록의 list다. 이미지 결과는 2,233건이다. 크기 분포: p50 535B / p90 4.1KB / p99 427KB / 최대 2.9MB.
- `sessionId`는 메인 파일 이름과 100% 같다. 한 프로젝트 폴더에 cwd가 여러 개인 경우가 9/43건이다.
- 그 밖의 쓸 만한 메타: `cost-state`(세션별 `totalCostUSD`, 모델별 토큰), 드물게 `quotaLimits.{status,resetsAt,rateLimitType}`(5건), `attributionSkill/Agent/McpServer`.

## 결정

1. **언어·의존성**: Python 3.12 + uv. 코어는 표준 라이브러리만 쓴다(`json`, `sqlite3`, `pathlib`, `http.server`). LLM SDK와 HTTP 클라이언트는 의존성에 넣지 않는다.
2. **수집기(`collect`)**: `projects/**/*.jsonl`을 훑고 `journal.jsonl`은 제외한다. 파일별로 `(path, size, mtime, offset)`을 저장해 증분으로 읽는다.
   - 마지막 개행까지만 소비해 쓰다 만 줄을 버린다.
   - `size < offset`이면 파일이 교체된 것으로 보고 0부터 다시 읽는다. 중복 제거가 있어 다시 읽어도 안전하다.
   - 로그는 읽기 전용으로만 연다.
3. **호출 단위 = `message.id`**(없으면 `requestId`, 그다음 `uuid`). 전역으로 upsert하고, `output_tokens`는 **max**를 취한다. 나머지 필드는 첫 기록을 쓴다.
4. **calls 표 컬럼**(숫자·메타만): ts, session_id, project_dir, agent_id, is_sidechain, entrypoint, model, input, output, cache_read, cache_5m, cache_1h, thinking, web_search_n, web_fetch_n, tool_names(이름 목록), stop_reason, trigger(`human`/`tool_result`/`other`), prev_result_bytes, prev_result_has_image.
   - 직전 도구 결과 크기는 같은 파일에서 바로 앞 `user` 줄의 `tool_result` 블록 크기 합계(JSON 직렬화 바이트)로 계산한다.
   - `title`, `last-prompt`, `content`, `wireToolInputs` 같은 **본문 계열 필드는 읽어도 저장하지 않는다.**
5. **집계**: 5분 버킷 표(project, session, 사용량 단위·토큰 종류별 합계)와 events 표(사건, 근거 수치 JSON, 원인 코드, 템플릿 문장)를 둔다.
6. **분석·문구는 결정론적으로 만든다.** 원인 판정은 임계값 규칙, 문장은 `str.format` 템플릿이다.
7. **LLM 0회를 테스트로 강제한다.**
   - (a) 소스 AST를 검사해 `anthropic`, `openai`, `httpx`, `requests`, `urllib.request`, `socket`, `subprocess`의 `claude` 호출을 금지한다.
   - (b) pytest에서 `socket.socket`을 막아 둔 상태로 수집부터 웹 생성까지 전 기능을 통과시킨다.
8. **웹과 상주 방식**: 보고서 §추천안대로 지휘부·사용자가 확정하기 전까지는 **정적 HTML 생성**을 기본으로 개발한다. 상주는 M5에서 확정한다.

## 근거

- message.id 중복 제거에서 max를 쓰는 이유: "첫 줄"만 취하면 21k 건의 output이 과소 집계된다. "합"을 쓰면 input/cache가 2~6배 과다 집계된다.
- 서브에이전트·워크플로를 빼면 사이드체인 줄(전체 줄의 60%)이 누락된다. 이번 급증 원인 유형 중 "서브에이전트·병렬"을 판정할 수도 없게 된다.
- 표준 라이브러리만 쓰면 의존성 공급망 위험이 없다. LLM·네트워크 차단도 검사하기 쉬워진다.

## 대안

- ccusage를 그대로 호출해 집계하는 방법: Node 의존이 생기고, 호출 단위의 도구·트리거 메타가 없어 원인 분석을 할 수 없다. 대조용으로만 쓴다(M1).
- 파일 감시(watchdog): 의존성이 늘어나고 Windows에서 이벤트 누락 위험이 있다. 1~2분 폴링으로 충분하다.

## 결과

- 로그 형식이 바뀌면 파서에서 모르는 키는 무시하고 통계 카운터만 남긴다. `collect --doctor`로 스키마 변화를 보고한다.
- 원본 본문이 DB에 없으므로 사건 타임라인에는 "도구 이름·크기·토큰"만 보인다. 이는 의도한 한계다.
