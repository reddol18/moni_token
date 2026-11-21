# ADR-0007: 지도 UI 패널 — "건드린 UI"만, 렌더링 방식별 정의와 OS UI 트리

- 상태: 채택 (issue https://github.com/reddol18/moni_token/issues/16, 2026-11-21)
- 관련: ADR-0004(경로·서버), ADR-0004 추가 결정(이슈 https://github.com/reddol18/moni_token/issues/15, 검색 범위)

## 맥락

지도의 "UI 페이지" 패널은 원래 **에이전트가 지금 바꾸고 있는 UI**를 보여 주려던 것이다. 실제로는 트리에 있는
페이지 같은 파일을 전부(`*.html` 전부 + Next.js 라우트 전부) 나열했다. fund_agent에서는 아무도 손대지 않은
`backup/artifacts-*/…html` 수십 개가 패널을 채워서 아무 정보도 주지 못했다.

UI는 프로젝트가 화면을 그리는 방식에 따라 단위가 다르다. 정적 HTML은 파일, 웹 앱은 URL(라우트), 데스크톱 앱은
창 안의 컨트롤이다.

## 결정

### 1. "건드림"의 정의 (공통)

- 지도 창(window) 안의, 그리고 `?at=` 시점까지의 **읽기·수정 touch**만 센다(ADR-0004 추가 결정의 표에서 `read`, `edit`).
  Grep/Glob/LS 검색 범위(`search`)는 세지 않는다. Bash 등 경로가 없는 호출도 세지 않는다.
- 패널은 이 touch에서 **만들어진다**. 트리에서 찾은 페이지·라우트 목록은 "이 파일이 어느 UI에 속하나"를 찾는
  **조회표**로만 쓴다.
- 항목마다 건드린 에이전트(색 점)와 시각(툴팁 `도구 · 에이전트 · 시각`)을 붙이고, 누르면 그 파일을 소스 트리에서 보여 준다.
- 빈 상태 두 가지: 조회표·데스크톱 UI 소스가 하나라도 있으면 "이 기간에 건드린 UI 없음", 하나도 없으면
  "이 프로젝트에서 UI를 찾지 못함".

### 2. 렌더링 방식별 UI 단위

| 방식 | UI 단위 | 어떤 touch가 그 UI를 건드린 것인가 |
|---|---|---|
| 정적 | HTML 파일(`*.html`, `*.htm`) | 그 파일을 읽거나 고침 |
| Next.js app 라우터 | 라우트 URL(`app/(g)/cart/page.tsx` → `/cart`) | 라우트 파일, 그 폴더 안의 파일(같이 둔 컴포넌트), 라우트 파일이 상대 경로·`@/`로 import하는 파일(1단계) |
| Next.js pages 라우터 | 라우트 URL(`pages/about.tsx` → `/about`, `api/` 제외) | 라우트 파일, 그 파일이 import하는 파일(1단계) |
| SvelteKit | 라우트 URL(`src/routes/blog/[slug]/+page.svelte` → `/blog/[slug]`) | 라우트 폴더 안의 파일(`+page.ts`, `+page.server.ts`, 같이 둔 컴포넌트), import 1단계 |
| Flask/Jinja | 라우트 URL(`@app.route("/x")` 함수가 `render_template("x.html")`) | 그 템플릿 파일. 라우트를 못 찾은 템플릿은 정적 HTML처럼 파일 자체 |
| 데스크톱(WPF/WinUI/Avalonia XAML) | XAML 요소(`x:Name`/`Name`/`AutomationProperties.AutomationId`) | `.xaml`을 건드리면 그 파일의 이름 붙은 요소 전부, 코드 비하인드 `.xaml.cs`를 건드리면 그 파일 본문에 이름이 나오는 요소 |
| 데스크톱(WinForms) | 컨트롤(`*.Designer.cs`의 `this.x = new T()`와 `Controls.Add` 계층) | `.Designer.cs`면 전부, 같은 폼의 `Form1.cs`면 본문에 이름이 나오는 컨트롤 |

- import 1단계: 라우트 파일 본문에서 `from './x'`·`from '../x'`·`from '@/x'`(`@/`는 루트, 없으면 `src/`)만 따라가고
  확장자(`.tsx .ts .jsx .js .svelte`, `/index.*`)를 붙여 실제 파일을 찾는다. 2단계 이상과 tsconfig paths는 하지 않는다(비용과 오탐).
- 조회표를 만들 때 읽는 소스(라우트 파일의 import, Flask `.py`, XAML, Designer)는 **요청 때 읽고 화면에만 보낸다.** 저장하지 않는다.
  Flask `.py`는 touch된 파일 중 템플릿이 있을 때만 훑는다(파일 300개·각 512KB 상한).
- React Router 라우트 표·Nuxt·Django는 이번에 하지 않는다(다음 후보). 컴퓨터 사용·UI MCP 도구가 겨냥한 요소 표시도
  하지 않는다: 입력을 저장하지 않으므로(ADR-0004) 어떤 요소를 겨냥했는지 알 수 없다.

### 3. 데스크톱: 소스 쪽 요소 트리 (항상)

XAML·Designer 파일을 요청 때 파싱해 요소 트리(컨트롤 종류 + 이름)를 만들고, 건드린 요소에 체크를 단다.
이름은 개발자가 붙인 코드 식별자다. 화면 글자(Text/Content)는 읽지 않는다.

### 4. 데스크톱: 실행 중인 창의 Windows UI Automation 트리 (선택, 기본 꺼짐)

- **설정**: `~/.moni_token/config.toml`의 `[map] uia = true`. 기본은 꺼짐. 창 제목으로 고르고 싶으면 `uia_window = "제목 일부"`.
- **라이브러리**: 새 의존성 없음. Windows에 들어 있는 **네이티브 UI Automation COM API(UIAutomationCore)**를,
  `[ComImport]` 인터페이스 세 개(IUIAutomation·IUIAutomationTreeWalker·IUIAutomationElement의 필요한 슬롯만)를 PowerShell
  `Add-Type`으로 컴파일해 **PowerShell 자식 프로세스**에서 부른다(`powershell -NoProfile -NonInteractive -EncodedCommand`,
  `task.py`·`notify.py`와 같은 방식, `test_no_llm`의 subprocess 허용 목록에 `uia.py` 추가). 컨트롤 종류는 OS 언어로 번역된
  이름 대신 ControlType id(50000=Button …)를 받아 영어 이름으로 바꾼다.
  - 처음엔 .NET 관리형 `System.Windows.Automation`을 썼다. 실측(Windows 10, 메모장): 자식 요소가 전부 `Pane`으로 나오고
    제목 표시줄·메뉴가 빠졌다(Win32 클라이언트 쪽 프록시 문제). 네이티브 API는 같은 창에서 Edit·StatusBar·TitleBar·MenuBar를
    제대로 돌려줬다. 그래서 바꿨다.
  - 대안 `comtypes`(+UIAutomationCore): 프로세스 안이라 빠르지만 새 의존성이고, 첫 사용 때 `comtypes.gen`에 래퍼 파일을 쓴다.
    COM 호출이 걸리면 지도 서버 스레드가 같이 멈춘다.
  - 대안 `pywinauto`/`uiautomation`: 의존성이 크다(pywin32·comtypes 포함).
  - 대안 Python ctypes로 COM vtable 직접 호출: 의존성은 없지만 코드가 길고 틀리기 쉽다.
  - PowerShell은 프로세스가 분리돼 걸려도 시간 제한(10초)으로 끊고 지도는 계속 돈다. 대신 시작·컴파일 비용이 든다(아래).
- **어느 창**: (1) 실행 파일 경로가 **프로젝트 루트 아래**인 프로세스(예: `bin/Debug/App.exe`) 중 메인 창이 있는 첫 프로세스,
  (2) 없으면 `uia_window`가 제목에 들어간 창. 둘 다 없으면 "이 프로젝트의 실행 중인 창 없음". 다른 앱 창을 임의로 고르지 않는다.
- **범위**: `ControlViewWalker`로 깊이 8, 요소 400개까지. 요소마다 컨트롤 종류·AutomationId·이름(80자 자름).
- **체크**: 요소의 AutomationId가 건드린 소스 요소 이름(3절)과 같으면 체크(WPF는 `x:Name`, WinForms는 컨트롤 `Name`이
  기본 AutomationId).
- **비용**: 읽기 한 번이 PowerShell 시작·Add-Type 컴파일 포함 약 0.6초(실측: WinForms 창 요소 10개 622ms, 창 없음 ~0.6초).
  결과를 메모리에 **15초** 둔다(지도는 3초마다 묻는다). 서버에서는 **백그라운드 스레드**로 새로 읽고, 요청은 직전 결과(처음엔
  "읽는 중")를 바로 받는다. 페이지 응답이 PowerShell을 기다리지 않는다. 설정이 꺼져 있거나, 프로젝트에 데스크톱 UI 소스가 없고 `uia_window`도 없거나, `?at=` 과거 시점이면 부르지 않는다(과거 창은 읽을 수 없다).
- **개인정보**: 요소 이름·창 제목에는 사용자 내용(문서 제목, 입력값)이 들어갈 수 있다. **화면에만 보내고 저장하지 않는다**
  (DB·로그·정적 리포트 없음, 15초 메모리 캐시만). 창 제목 설정값은 명령줄이 아니라 환경 변수로 자식 프로세스에 넘긴다.
  127.0.0.1에서만 열린다(ADR-0004).
- macOS Accessibility·Linux AT-SPI는 하지 않는다(다음 후보).

## 결과

- 패널은 기간 안에 건드린 UI만 보여 준다. fund_agent처럼 HTML이 많아도 고친 파일 하나면 항목 하나.
- `/api/state`의 `pages`는 이제 건드린 UI 항목 목록이고, 데스크톱 트리는 `ui`에 들어간다. 저장 스키마는 바뀌지 않는다.
- 조회표·import·XAML·Designer 파싱은 요청마다 다시 한다(캐시 없음). 실측 비용은 이슈 보고에 적는다.
