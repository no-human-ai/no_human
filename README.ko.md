<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

**티켓에서 리뷰까지 끝낸 풀 리퀘스트로.**<br>***무료 오픈소스, 로컬에서 실행.***

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · **한국어**

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![Open Source Helpers](https://www.codetriage.com/no-human-ai/no_human/badges/users.svg)](https://www.codetriage.com/no-human-ai/no_human) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [퀵스타트](docs/quickstart.md) · [문서](docs/README.md) · [스프린트 하나를 통째로 처리하는 모습 보기](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![macOS용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Windows용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Linux용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="no_human 보드: 태스크 하나는 질문의 답을 기다리며 ‘답변 필요’에 대기 중, 네 개는 병렬로 진행 중, 풀 리퀘스트 하나는 리뷰 대기 중." width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">전체 흐름 보기</a> — 티켓이 들어가면 리뷰를 마친 풀 리퀘스트가 나옵니다. 전 과정 57초.</sub>

</div>

> 이 문서는 영문 README의 번역본입니다. 내용이 다를 경우 [영문판](README.md)이 기준입니다. 링크된 문서는 현재 모두 영문입니다.

<ins>**믿고 맡길 수 있는**</ins> AI 코딩 팩토리:

- **코드보다 계획이 먼저.** 티켓 내용과 저장소에서 직접 찾아낸 정보를
  바탕으로 계획을 세웁니다. 계획 생성에 실패하면 코더에게 계획 없이
  작업 중이라고 알립니다. 변경이 사소하다고 판정되면 계획을 건너뛰되
  코더에게는 알리지 않습니다 — 의도된 설계이며, 건너뛴 사실 자체는 해당
  실행의 이벤트 스트림에 남습니다.
- **반박을 전제로 한 리뷰.** 다른 모델이 코더의 대화 기록을 전혀 본 적
  없는 새 세션에서 "완료"라는 주장을 반박하라는 지시를 받고 리뷰합니다.
  결과물은 파일과 줄 번호를 인용한 통과/실패 체크리스트이며, 모델이
  숫자로 매긴 자체 점수가 아닙니다.
- **변조 가드.** 삭제된 테스트, 새로 추가된 skip, 항상 참이 되도록 바뀐
  단언문 — 리뷰 게이트가 돌기 전에 기계적으로 집계되고, 인수 조건에
  비추어 정당화하지 못하면 그 시도는 거기서 중단됩니다.
- **수정이 정말 그 버그를 고쳤다는 증명.** 증거로 제출되는 테스트는 머지
  베이스에서는 실패하고 새 트리에서는 통과해야 합니다 — 재현 게이트가
  양쪽을 모두 실행합니다. 기본값으로는 Python 버그 수정에 적용되고,
  `repro_gate.mode: required`로 모든 태스크 유형과 모든 변경에
  적용됩니다.
- **테스트가 실제로 돌아갑니다.** 로컬에서, 원하면 CI를 통해서도 —
  테스트 명령을 찾지 못한 PR에는 본문에 그대로 **NOT RUN**이라고
  표시됩니다.
- **정직한 중단.** 끝낼 수 없으면 멈추고 이유를 말합니다 — 답이 있으면
  풀리는 상황이라면 구체적인 질문을, 단순히 예산이 바닥난 것이라면
  구조화된 기록을 남깁니다. 그럴듯한 diff를 지어내는 일은 없습니다.

## 설치

어떤 방식으로 설치하든 **Claude 자격 증명**이 필요합니다.
`claude setup-token`으로 발급하는 OAuth 토큰(개인 구독 또는
엔터프라이즈)입니다. 이 명령을 쓰려면 Claude Code CLI가 있어야 하니 먼저
설치하세요 —
`npm install -g @anthropic-ai/claude-code`, 또는
`curl -fsSL https://claude.ai/install.sh | bash`. 데스크톱 앱도 태스크마다
이 CLI를 호출합니다. Anthropic에 직접 결제하려면
`llm.auth_mode: "api_key"`를 설정하고 `ANTHROPIC_API_KEY`를
`~/.no_human/.env`에 넣으세요.

### 한 줄 설치 (CLI + 보드)

```bash
uv tool install no-human   # 또는 pipx install no-human — wheel에 보드가 들어 있습니다
nh init && nh doctor       # 토큰, 설정, 첫 저장소. 그리고 설치가 실제로 동작하는지 검증
```

### 데스크톱 앱

[![macOS용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Windows용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Linux용 다운로드](https://img.shields.io/badge/%EB%8B%A4%EC%9A%B4%EB%A1%9C%EB%93%9C-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

릴리스마다 아티팩트와 함께 SHA-256 체크섬을 제공합니다. 플랫폼별 참고
사항과 첫 실행 안내는 [docs/quickstart.md](docs/quickstart.md)에 있습니다.

### 소스에서 설치

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # `nh` 엔트리 포인트를 .venv에 설치
(cd web && npm install && npm run build)   # 보드 빌드(첫 설치는 몇 분 걸릴 수 있음)
uv run nh init          # 토큰, 설정, 첫 저장소(약 2분)
uv run nh doctor        # 믿고 쓰기 전에 설치가 실제로 동작하는지 검증
```

보드를 쓰려면 `web` 빌드는 생략할 수 없습니다. 소스 체크아웃에는
`web/dist`가 없어서, 빌드하지 않으면 `nh start`는 API만 제공하고 UI는
렌더링되지 않습니다. Python 3.12+,
[uv](https://github.com/astral-sh/uv), git, 그리고 보드 빌드용으로 npm이
포함된 Node가 필요합니다.

## 제품 하이라이트

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>코드보다 계획이 먼저</h3>
      <p>티켓 내용과 저장소에서 찾아낸 정보로, 직접 확인할 수 있는 인수 조건을 씁니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="태스크의 계획: 이해한 내용으로서의 인수 조건 세 가지, 바꿀 파일 두 개, 접근 방식, 테스트 계획, 범위 밖 항목, 검증 명령." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>독립된 리뷰어</h3>
      <p>코더의 세션을 전혀 본 적 없는 다른 모델이 “완료”를 반박하라는 지시를 받고 리뷰합니다. 통과 또는 실패를 판정하고, 차단 지적은 모두 파일과 줄 번호를 인용합니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="리뷰어의 판정: PASSED. 인수 조건마다 그것을 충족하는 파일과 줄 번호가 붙은 체크 표시, 그리고 해당 diff가 붙은 비차단 지적 하나." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>당신의 테스트, PR 본문에</h3>
      <p>로컬에서, 또는 CI를 통해 실행합니다. 테스트 명령을 찾지 못하면 <b>NOT RUN</b>이라고 적히며, 빈칸으로 남지 않습니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="태스크의 Test results 패널: CLEAN, 5개 중 5개 통과, 아래에 pytest 출력." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>변조 가드</h3>
      <p>삭제된 테스트, 새로 추가된 skip, 항상 참이 된 단언문을 리뷰 전에 집계합니다. 정당화하지 못하면 그 시도는 중단됩니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="중단된 시도: 빨간 TAMPER DETECTED 배너, 리뷰어 판정 FAILED, 그리고 “테스트 세 개가 삭제되었고 이를 정당화할 인수 조건이 없다”는 차단 지적." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>수정이 정말 그 버그를 고쳤다는 증명</h3>
      <p>증거로 제출되는 테스트는 이전 코드에서 실패하고 새 코드에서 통과해야 합니다. 게이트가 양쪽을 모두 실행하고, 판정은 이벤트 로그에 남습니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="태스크의 이벤트 로그: 테스트 통과, 상태 reviewing, 리뷰어의 변조 검사 none, 재현 게이트 pass (required), 이어서 lint, 커밋, 풀 리퀘스트 열림." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>정직한 중단</h3>
      <p>당신이 필요할 때는 추측하지 않고, 구체적인 질문 하나를 남기고 멈춥니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="보드의 Needs answer 레인: “Dedupe by user, or by digest id?”라는 질문을 안고 멈춰 있는 태스크 하나와 Answer question 버튼. 옆에는 Working 레인과 Review PR 레인." width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>쓰고 있는 트래커의 티켓을 그대로 보드로</h3>
      <p>백로그에서 Jira 또는 Linear 티켓을 고릅니다(monday.com 보드는 폴링으로 가져옵니다). 시작 전에 하나씩 당신과 함께 범위를 정합니다.</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="Jira에서 동기화된 Backlog: 일치하는 티켓 네 개가 선택되어 있고, Start 4 tasks 버튼." width="100%" />
    </td>
  </tr>
</table>

<sub>화면은 데모 워크로드를 올린 실제 보드입니다.</sub>

## 태스크 하나 돌려 보기

`nh`를 인자 없이 실행하면 셸이 열립니다. 레인 현황과 실시간 이벤트 로그,
태스크를 자연어로 설명해 등록하는 입력창이 함께 표시됩니다. 아래 명령은
모두 그대로 동작합니다.

```bash
nh                                   # 셸
nh start                             # 보드 + 워커, 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # 리뷰어의 증거 체크리스트
nh diff <id>                         # 반영하려는 diff
nh approve <id>                      # 여러분의 승인이 PR을 스쿼시 머지합니다(git.approve_identity)
nh reject <id> --reason "..."        # 피드백과 함께 반려
```

## 연동

이미 쓰고 있는 트래커를 no_human에 연결하면 티켓이 보드로 들어옵니다 —
트래커 필터는 설정 파일에 있지, 태스크 자신의 텍스트에는 절대 들어가지
않습니다. 통신 오류는 로그로 남기고 다음 폴링 주기에 재시도하며, 풀을
중단시키지 않습니다.

| 트래커 | 티켓이 들어오는 방식 | 설정하는 필터 |
|---|---|---|
| **Jira Cloud** | REST `search/jql` 폴링(HTTP Basic `email:token`) | `integrations.jira.jql` |
| **Linear** | GraphQL API 폴링 | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | GraphQL v2 폴링 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

쓰기 반영(`write_back`, 기본값은 꺼짐)을 켜면 티켓이 태스크와 함께
움직입니다. 상태 카테고리, 유형, 또는 지정한 라벨로 매칭하며(전환 ID를
하드코딩하지 않습니다), PR 링크도 함께 달립니다. 사람이 필요한 태스크에는
코멘트만 남기고, 상태를 전환하지는 않습니다. GitHub와 GitLab 이슈는 URL로
태스크로 가져올 수 있고, PR/MR은 직접 운영하는 호스트에 생성됩니다. 사람
확인이 필요한 태스크가 생기면 Slack과 Teams로 알림이 가고, Jenkins와
CircleCI로 테스트 레이어를 돌려 루프의 게이트로 쓸 수 있습니다. 각 설정
방법: [docs/adapters.md](docs/adapters.md).

**Jira 흐름을 처음부터 끝까지 보기** — Jira 보드에서 동기화한 티켓의
범위를 잡고, 구현하고, 리뷰를 통과한 풀 리퀘스트로 전달하기까지
(클릭하면 모든 단계가 담긴 전체 영상):

[![Jira 흐름 데모](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">전체 데모 재생</a></strong> — 1:33, Jira 보드에서 리뷰 통과 PR까지</p>

## MCP 서버 — 지금 쓰고 있는 에이전트에서 바로 일 넘기기

no_human에는 **MCP(Model Context Protocol) 서버**가 들어 있습니다. 공식
Python MCP SDK로 만든 stdio 브리지로, Claude Code나 Cursor 등 어떤 MCP
클라이언트에서든 로컬 no_human에 일을 맡기고 진행 상황을 확인할 수
있습니다.

```bash
nh mcp-serve        # MCP 서버(stdio)
```

도구는 딱 두 개뿐입니다:

| 도구 | 하는 일 |
|---|---|
| `task_add(title, description, repo_path)` | 태스크를 등록합니다. no_human이 계획을 세우고, 변경을 작성하고, 테스트를 돌리고, 두 번째 모델의 리뷰를 거쳐 풀 리퀘스트를 엽니다. |
| `task_status(task_id_or_external_id)` | 해당 태스크의 현재 상태를 반환합니다 — 상태, 시도 횟수, 그리고 PR 링크(생겼다면). |

로컬의 no_human(`http://127.0.0.1:8420`)과만 통신합니다. 그 주소는
localhost라서 인증이 없고, 중간에 no_human 측 서비스가 전혀 개입하지
않습니다. Claude Code에서는 같은 서버가 플러그인으로도 제공됩니다 — 이
저장소 자체가 플러그인 마켓플레이스라서, 아래 두 줄이면 세션에 두 도구가
나타납니다:

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

다른 MCP 클라이언트는 일반적인 stdio 엔트리를 쓰면 됩니다:

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## 문서

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | 0에서 첫 태스크까지, 플랫폼별 |
| [configuration.md](docs/configuration.md) | 모든 설정과 기본값 |
| [verification.md](docs/verification.md) | 게이트, 시도 횟수가 제한된 루프, 그리고 한계 |
| [security.md](docs/security.md) | 인증 경계, "머지 금지" 규칙, 각종 가드 |
| [blockers.md](docs/blockers.md) | 에스컬레이션, 웨이크 워처, `nh reply` |
| [adapters.md](docs/adapters.md) | 티켓 수집(intake), 컨텍스트, VCS/CI 백엔드 |
| [eval.md](docs/eval.md) | 골든 셋, 리플레이 채점, 섀도 모드 |
| [CHANGELOG.md](CHANGELOG.md) | 릴리스별 변경 사항 |

## 개발

```bash
uv sync
uv run pytest -q
uv run nh --help
```

이슈와 풀 리퀘스트를 환영합니다. 제출 전에 `uv run pytest -q`를 먼저
돌려주세요.

no_human 덕분에 리뷰 사이클을 한 번이라도 아꼈다면, 스타 하나가 다른
사람들이 이 프로젝트를 찾는 데 도움이 됩니다:
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## 커뮤니티

질문, 버그 리포트, 보여주고 싶은 실행 결과는 [Discord](https://discord.gg/mSARvj6yW6)로,
[r/no_human](https://www.reddit.com/r/no_human/) 게시글이나
[GitHub issue](https://github.com/no-human-ai/no_human/issues)로도 환영합니다.

## 라이선스

MIT — [LICENSE](LICENSE)를 참고하세요. 라이선스가 다루는 것은 코드이지
이름이 아닙니다. "no_human" 이름과 로고 사용에 관한 정책은
[TRADEMARK.md](TRADEMARK.md)에 있습니다. 바이너리로 패키징하면 소스
트리에는 없는 의무가 따르며, 목록은
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)에 있습니다.
