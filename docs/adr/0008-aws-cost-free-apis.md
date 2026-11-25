# ADR-0008: 상태줄의 AWS 비용 — 네트워크 예외, 무료 API만, 캐시, 허용 목록

- 상태: 채택 (issue https://github.com/reddol18/moni_token/issues/18, 2026-11-25)
- 관련: CLAUDE.md 절대 규칙 2(LLM 0회)·3(아무것도 밖으로 보내지 않음), ADR-0004(127.0.0.1 서버)

## 맥락

사용자는 상태줄에 Claude Code 사용량과 함께 **이 AWS 계정의 이번 달 예상 요금**을 10분 간격으로 보고 싶어 한다.
조건은 하나: **이 기능은 돈이 들면 안 된다(사용자, 2026-11-25).**

CLAUDE.md 규칙 3은 네트워크 전송을 금지한다. 이 기능은 본질적으로 AWS에 묻는 일이라 예외가 필요하다.

조사(2026-11-25, AWS 공식 문서):

| 방법 | 돌려주는 것 | 비용 | 갱신 | 비고 |
|---|---|---|---|---|
| CloudWatch `GetMetricStatistics` — `AWS/Billing` `EstimatedCharges` (`Currency=USD`) | 계정 전체의 이번 달 누적 예상 요금 | 상시 무료 API 요청 월 100만 회. `GetMetricData`는 제외되어 항상 유료 | 하루 여러 번 | us-east-1에만 있음. **Receive Billing Alerts**를 켜야 하고 다시 끌 수 없음. Organizations에서는 지불 계정만 |
| AWS Budgets `DescribeBudget` | `CalculatedSpend.ActualSpend`, `ForecastedSpend` | 작업(action) 없는 예산은 무료, 가격표에 API 요금 없음 | 하루 최대 3번 | 예상값은 약 5주 이력 필요. 예산을 미리 하나 만들어야 함 |
| Cost Explorer API (`aws ce …`) | 누적, 예상 | **요청당 $0.01, 무료분 없음**(콘솔은 무료) | 하루 | **금지** |

출처:
- https://aws.amazon.com/cloudwatch/pricing/
- https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/monitor_estimated_charges_with_cloudwatch.html
- https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/
- https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CalculatedSpend.html
- https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html
- https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/
- https://docs.aws.amazon.com/service-authorization/latest/reference/list_awsbudgetservice.html

확인하지 못한 것(공개 전 다시 확인하고 날짜를 여기에 적는다):
- Budgets API 호출이 무료라는 명시적 문장 (가격표에 API 요금 항목이 없다는 것만 확인)
- 무료분을 넘은 CloudWatch API 단가(가격표 예시에서 1,000건당 약 $0.01로 추정)
- `EstimatedCharges`의 정확한 게시 주기
- STS `GetCallerIdentity` 무료(권한도 필요 없음) — AWS 문서의 일반 설명에 근거

## 결정

### 1. 네트워크 예외의 범위

- **선택 기능, 기본은 꺼짐.** `config.toml`의 `[cloud] enabled = true`일 때만 동작한다. 꺼져 있으면 프로세스를 하나도
  띄우지 않는다(테스트: `subprocess.run/Popen/call/check_output`을 막은 채로 갱신기·페이지·상태줄이 돈다).
- 나가는 것은 **AWS CLI가 하는 읽기 전용 호출 세 종류**뿐이다. moni_token의 로컬 데이터(로그·토큰 수·경로)는 보내지 않는다.
  moni_token 자신은 소켓을 열지 않는다(`test_no_llm`의 금지 모듈 검사는 그대로, `subprocess`만 `cloud.py`에 허용).
- LLM은 여전히 0회. 판정·문장은 모두 코드와 템플릿.

### 2. 허용 목록 (`cloud.guard`)

| 서비스 | 작업 | 용도 |
|---|---|---|
| `cloudwatch` | `get-metric-statistics` | `EstimatedCharges` 최신값 |
| `budgets` | `describe-budget` | `ActualSpend`, `ForecastedSpend`, 한도 |
| `sts` | `get-caller-identity` | `describe-budget`에 필요한 계정 ID (메모리에만) |

- `aws ce`(서비스 전체), `get-metric-data`(인자 어디에 있든), 그 밖의 모든 작업은 `NotAllowed`로 거부하고 프로세스를 띄우지 않는다.
- 테스트가 `cloud.py`의 AST를 읽어 모든 `.call([...])` 호출 지점의 앞 두 인자가 허용 목록 안인지, `subprocess` 사용이
  `_default_run` 한 곳뿐인지 검사한다. 다른 모듈에는 `get-metric-statistics`, `describe-budget` 문자열 자체가 없어야 한다.
- MCP가 아니라 AWS CLI: 상태줄은 셸 명령이라 MCP를 부를 수 없고, AWS 비용 MCP 서버들은 유료 Cost Explorer를 쓴다.

### 3. 주기와 상한

- `EstimatedCharges`: **10분**마다(`metric_min`, 최소 5) — 월 약 4,320회, 무료분의 0.4%.
- `DescribeBudget`: **6시간**마다(`budget_h`) — 월 약 120회. 데이터가 하루 최대 3번 바뀐다.
- STS: 프로세스 시작 후 처음 한 번, 그리고 점검 때. 계정 ID는 프로세스 메모리에만 둔다.
- 월 호출 상한 `max_calls_month`(기본 6,000, 세 API 합계, UTC 달 기준). 넘으면 다음 달 1일(UTC)까지 아무것도 부르지 않는다.
- 실패하면 지수 백오프: 주기 × 2^실패 횟수, 최대 24시간. 성공하면 원래 주기로.
- 무료분은 계정의 다른 CloudWatch API 사용과 공유된다 → 점검 결과와 README에 월 예상 호출 수와 확인 위치(Billing → Free Tier).

### 4. 캐시와 상태줄 — 상태줄은 절대 기다리지 않는다

- 갱신기는 `moni_token map`의 백그라운드 데몬 스레드(`cloud.run_forever`, 30초마다 설정을 다시 읽음). **지도 작업이 도는 동안에만** 동작한다.
  대시보드에서 켜고 끄면 재시작 없이 반영된다.
- 결과는 `~/.moni_token/cloud_cost.json`(원자적 교체). 저장하는 것: 금액, 통화, 데이터 시각, 가져온 시각, 월 호출 수, 오류 코드, 마지막 점검 결과
  (설정의 해시만, 이름은 없음).
- 상태줄은 이 파일과 `config.toml`만 읽는다. 예: `AWS $0.88 (fc $1.08) · 2h` / `AWS $0.88 (예상 $1.08) · 2시간`. 나이는 AWS가 지표를
  게시한 시각 기준. 꺼짐이거나 마지막 성공이 12시간보다 오래되면 숨긴다. 데이터 없이 최근 실패만 있으면 `AWS ?`.
- 예상값: Budgets `ForecastedSpend`만 쓴다. 없으면(이력 5주 미만, 예산 없음) 예상을 표시하지 않는다. 로컬 직선 외삽은 하지 않았다(아래 대안).
- Organizations 멤버 계정처럼 청구 지표가 없으면 예산의 `ActualSpend`를 누적값으로 쓴다.

### 5. 자격 증명과 개인정보

- 기존 AWS CLI 프로필을 쓴다. moni_token은 키를 읽지도, 저장하지도, 기록하지도 않는다. 설정 안내에서 "키를 Claude나 페이지에 붙여 넣지 말 것"을 명시.
- 계정 ID, ARN, AWS 오류 메시지(계정 ID가 들어 있을 수 있음)는 저장·표시하지 않는다. 오류는 코드(`AccessDenied` 등)만 남긴다.
- 최소 IAM: `cloudwatch:GetMetricStatistics`, `budgets:ViewBudget`(그 예산 ARN 하나), `aws-portal:ViewBilling`.

### 6. 대시보드에서 켜고 끄기 (사용자 요구, 2026-11-25)

- 사용량 페이지 맨 아래 **AWS 비용** 카드: 켜짐/꺼짐, 프로필·예산 이름·`aws_path` 입력, **점검**, **켜기/끄기/저장**.
  꺼져 있으면 1회 설정 안내(en/ko 카탈로그)와 "왜 0원인가"를 펼쳐 보여 준다. 켜지면 위 타일 줄에 "이번 달 AWS" 타일.
- **config.toml이 원본.** 대시보드는 `[cloud]` 표만 다시 쓰고 다른 줄(주석 포함)은 그대로 둔다. 쓰기 전 `tomllib`로 다시 읽어 확인.
- **점검을 통과해야 켤 수 있다.** 같은 설정(해시)으로 통과한 점검이 없으면 켜기·설정 변경을 409로 거부.
- 점검 = `moni-token cloud check`: aws 찾음 → 프로필(STS) → 청구 지표 → 예산 → 월 호출 수. 단계마다 성공/실패와 도움말. 최대 3회 호출.
  통과한 점검의 값은 캐시에 넣어, 켠 직후 바로 같은 호출을 다시 하지 않는다.
- POST 엔드포인트(`/api/cloud/check`, `/api/cloud/settings`) 보안: 127.0.0.1 바인딩(기존), Host가 이 서버(포트까지)여야 함,
  Origin이 같은 서버여야 함(없으면 거부), `Sec-Fetch-Site`가 있으면 `same-origin`, 서버 프로세스마다 새로 만든 토큰을
  `X-Moni-Token`으로(페이지에 심음, `hmac.compare_digest`), `Content-Type: application/json`, 본문 4KB 이하, 알려진 필드만
  (`enabled`, `aws_profile`, `budget_name`, `aws_path`), 값 검증(프로필 `[A-Za-z0-9_.+=,@-]{0,64}`, 예산 이름
  `[A-Za-z0-9 _.+=,@()-]{0,100}`, `aws_path`는 실제로 있는 `aws.exe`/`aws`의 절대 경로). 명령은 셸 없이 인자 목록으로 실행.
- 정적 `report.html`에는 폼 코드·토큰·설정이 들어가지 않는다(요청 코드 `fetch(` 자체가 없음). 값과 "라이브 대시보드에서 켜라"는 안내만.

### 7. AWS CLI 찾기

`aws_path`(설정) → PATH → 일반 설치 폴더(`%ProgramFiles%\Amazon\AWSCLIV2\aws.exe` 등). 지도 작업처럼 CLI 설치 전에 시작된
프로세스는 새 PATH를 모르기 때문. 프로세스는 `CREATE_NO_WINDOW`, `AWS_PAGER=""`, `--no-cli-pager`, 30초 제한.

## 대안

- **Cost Explorer API**: 가장 정확하고 서비스별 분해도 되지만 요청당 $0.01 — 10분 주기면 월 약 $43. 금지.
- **CloudWatch `GetMetricData`**: 한 번에 여러 지표를 받지만 무료분 없음. 금지.
- **로컬 직선 외삽 예상값**: 이슈에서 선택지였으나 Budgets 예상값과 섞이면 혼란스러워 이번에는 넣지 않았다. 필요하면 "추정" 표시를 붙여 추가.
- **상태줄에서 직접 호출**: 상태줄이 AWS를 기다리게 되고 호출 수가 상태줄 갱신 빈도에 묶인다. 캐시 방식으로.
- **별도 예약 작업**: 2분 `check` 작업에 붙일 수도 있었으나 사용자 지시대로 지도 작업 안에서만 돈다(지도가 꺼지면 데이터가 낡아 12시간 뒤 숨김).

## 결과

- 실제 계정 확인(2026-11-25): 점검 1회 = STS 1 + CloudWatch 1 + Budgets 1. `EstimatedCharges` 0.88 USD, `ActualSpend` 0.883,
  `ForecastedSpend` 1.08, 상태줄 `AWS $0.88 (예상 $1.08) · 40분`.
- 테스트: 가짜 `aws`(파이썬 스크립트와 프로세스 내 대역)로 파싱, 낡은 캐시, 실패와 백오프, 상한, 꺼짐 = 프로세스 0, 허용 목록,
  POST 보안, 설정 왕복, en/ko 문자열. 실제 AWS 호출 없음.
