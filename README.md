# Stock-Mining

**미국 주식(NYSE·NASDAQ)** 스윙 트레이딩 연구 루프 (`market: us`, 기본값. `market: kr`로 한국장도 가능). 매일 데이터를 모으고, 여러 트레이더의 규칙을 백테스트와
페이퍼 트레이딩으로 검증하며, Claude가 리포트와 원칙 수정안을 냅니다. **주문은 사람이 직접** 하고, Slack으로
매매일지를 남기면 다음 분석에 반영됩니다.

```
데이터 수집 ──▶ 저장소(SQLite) ──▶ 전략 엔진 ──▶ Claude 분석 ──▶ Slack ──▶ 나(판단·주문)
FinanceDataReader   시세·일지·원칙     신호·백테스트·      리포트·원칙 제안     알림·명령     │
                         ▲            페이퍼 트레이딩                                        │
                         └──────────────────── /buy /sell 매매일지 ◀─────────────────────────┘
```

## 스스로 학습하는 구조

| 층 | 무엇을 하나 | 사람 승인 |
|---|---|---|
| 페이퍼 트레이딩 | 모든 전략(음소거된 것 포함)의 상위 신호를 매일 가상 매매해 실제 성과를 쌓음 | 불필요 |
| 학습기(learner) | 백테스트 성과를 사전확률로, 페이퍼 성과를 증거로 삼아 **시장 국면별** 사후 기대값 계산 → 전략 가중치, 음소거/재활성화 | 불필요 |
| 파라미터 튜닝 | 매주 워크포워드(구간 내 최적화 → 표본 외 검증). 표본 외에서 기존보다 나을 때만 채택 | `auto_adopt_params`로 선택 |
| Claude 리서치 | 매주 주제를 돌아가며 웹 검색(Minervini, O'Neil, Connors, 터틀, 국내 트레이더 등) → 규칙 DSL로 변환 → 백테스트 게이트 | **필수** (`/approve`) |
| Claude 일일 리뷰 | 신호·일지·원칙 검토, "최근 10건 중 손절 미준수 4건" 같은 지적, 원칙 수정안 제안 | **필수** |
| 아침 브리핑 | 웹 검색으로 간밤 미국장·후보 종목 뉴스·공시 확인 → 위험 종목 '보류' 표시 | 불필요(정보성) |

Claude가 만든 규칙은 [features.py](src/stockmining/features.py)의 화이트리스트 기반 식 언어로만 표현되므로
임의 코드가 실행되지 않습니다. 모든 규칙에는 손절이 강제됩니다.

## 일정 (뉴욕 시간 기준 타이머 → 서머타임 자동 반영)

| 뉴욕 시간 | 한국 시간 (서머타임 / 표준시) | 작업 |
|---|---|---|
| 월~금 17:30 | 화~토 06:30 / 07:30 | `evening` — 시세 갱신(약 1,600종목, 10~15분) → 페이퍼 갱신 → 신호 → 학습 → 추천 → Claude 리포트 |
| 월~금 08:30 | 월~금 21:30 / 22:30 | `morning` — 개장 전 브리핑(웹 검색): 선물·금리·뉴스·**실적 발표일** 확인, 보류 종목 표시 |
| — | 토 12:00 | `weekly` — 파라미터 튜닝, 백테스트 갱신, Claude 리서치 → 새 규칙 후보 |
| 상시 | 상시 | `bot` — Slack 명령 처리 |

아침에 일어나면 저녁 리포트(그날 밤 매수 후보)가 와 있고, 개장 1시간 전에 브리핑이 한 번 더 옵니다.
휴장일은 `exchange_calendars`(XNYS)로 건너뜁니다. 실패하면 Slack으로 알림이 갑니다.

## Slack 명령

봇에게 DM으로 보내거나(앞의 `/` 없이), 아무 채널에서 `/sm` 뒤에 붙여 씁니다. 응답은 본인(SLACK_USER_ID)에게만 합니다.

```
buy NVDA 180.5 0.5 눌림목 매수      매수 기록 (소수점 수량 가능, 최근 추천 종목이면 손절·목표가 자동 연결)
sell NVDA 195 0.5 목표가 도달       매도 기록
stop NVDA 168                       손절가 수정
pos  recs  strats  report           포지션·추천·전략 현황·최근 리포트
insights  edge                      학습 일지·엣지 맵
proposals  approve 3  reject 3      제안 확인·승인·거절
ask 반도체 업황 어때?                Claude에게 질문(웹 검색 포함)
/sm pos                             (DM 대신 슬래시 명령으로)
```

텔레그램을 원하면 `notifier: telegram` + TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.

### Slack 앱 만들기 (5분)
1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From an app manifest** → 워크스페이스 선택
2. [deploy/slack-app-manifest.yaml](deploy/slack-app-manifest.yaml) 내용을 붙여넣고 생성 → **Install to Workspace**
3. **OAuth & Permissions**의 `Bot User OAuth Token`(xoxb-...) → SLACK_BOT_TOKEN
4. **Basic Information → App-Level Tokens → Generate**, scope `connections:write` → `xapp-...` → SLACK_APP_TOKEN
5. Slack에서 내 프로필 → ⋮ → **멤버 ID 복사**(U...) → SLACK_USER_ID

## AWS 배포

**추천 구성: EC2 t4g.small 1대(서울) + S3 + SSM.** 월 약 $17 (EC2 ~$15, EBS 20GB ~$2, S3 소액).

- **EC2 (t4g.small)**: 전 종목 백필·백테스트에 메모리 2GB가 필요하고, Slack 봇이 상시 돌아야 해서
  Lambda보다 단순합니다. 리전은 서울(기본)이면 충분합니다. 미국 데이터(Yahoo)와 Anthropic API는 어디서든 잘 됩니다.
  미국 리전을 원하면 `terraform apply -var region=us-east-1`.
- **S3**: 릴리스 배포 + SQLite·원칙 파일 백업(작업마다, 버전 관리로 30일 보관).
- **SSM Parameter Store**: 토큰 보관(SecureString). **SSM Session Manager**로 접속하고, Slack은 Socket Mode(서버가 바깥으로 연결)라 인바운드 포트가 하나도 없습니다.

```bash
# 0) 비밀값 등록 (한 번만)
# Claude 분석은 Claude 구독(Pro/Max)으로: 이 Mac에서 `claude setup-token` → 나온 토큰을 등록 (API 키 불필요)
aws ssm put-parameter --region ap-northeast-2 --type SecureString --name /stockmining/CLAUDE_CODE_OAUTH_TOKEN --value '...'
# (유료 API를 쓰려면 대신 /stockmining/ANTHROPIC_API_KEY 등록. 둘 다 없으면 Claude 분석만 건너뜀)
aws ssm put-parameter --region ap-northeast-2 --type SecureString --name /stockmining/SLACK_BOT_TOKEN --value 'xoxb-...'
aws ssm put-parameter --region ap-northeast-2 --type SecureString --name /stockmining/SLACK_APP_TOKEN --value 'xapp-...'
aws ssm put-parameter --region ap-northeast-2 --type SecureString --name /stockmining/SLACK_USER_ID  --value 'U...'

# 1) 인프라
cd deploy/terraform && terraform init && terraform apply && cd ../..

# 2) (선택) 운용 자금 등 설정
cp config.example.yaml config.yaml   # 수정 후 deploy 시 S3로 업로드됨 (git에는 안 올라감)

# 3) 배포 — git HEAD를 패키징해 S3 업로드 → SSM으로 설치. 처음엔 4년치 백필(약 15분)이 자동 실행
./deploy/deploy.sh

# 접속 / 로그
aws ssm start-session --target $(terraform -chdir=deploy/terraform output -raw instance_id)
journalctl -u stockmining@evening -n 100      # 서버에서
systemctl list-timers 'stockmining*'
```


## Lightsail(이미 쓰는 서버)에 배포

S3·IAM 없이 SSH로 올립니다. 토큰은 이 Mac에서 SSM으로 읽어 SSH로만 서버에 전달합니다(`/etc/stockmining/env`).
다른 서비스(docker)와 같이 쓰는 서버라 작업은 메모리 최대 1.3GB·CPU 1코어로 제한하고, 백업은 서버 안
`/var/lib/stockmining/backups/`에 최근 14일치를 남깁니다.

```bash
HOST=54.116.117.190 KEY=~/Downloads/LightsailDefaultKey-ap-northeast-2.pem ./deploy/lightsail.sh
ssh -i $KEY ubuntu@$HOST 'journalctl -u stockmining@init -f'      # 첫 배포 시 백필 진행 상황
```

**웹 대시보드(보기 전용)**: `WEB_DOMAIN=stock.54-116-117-190.sslip.io`를 붙여 배포하면 서버의 기존 Caddy(docker)에
블록을 추가해 HTTPS로 띄웁니다. sslip.io는 IP를 주소로 바꿔 주는 무료 DNS라 도메인 없이 됩니다. 아이디 `me`,
비밀번호는 서버 `/etc/stockmining/web.env`의 WEB_PASSWORD(처음 배포 때 자동 생성). DB는 읽기 전용으로 엽니다.

## 로컬 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
export SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... SLACK_USER_ID=...   # 없으면 콘솔 출력
.venv/bin/stockmining init            # 약 1,600종목 4년치 백필 + 기준 백테스트 (약 15분)
.venv/bin/stockmining evening --force
.venv/bin/stockmining test-rule my_rule.yaml
.venv/bin/stockmining simulate        # 워크포워드 모의투자: 전략 선택 방식 비교 + 1년 성과 확률 + 무작위 기준선
.venv/bin/stockmining cmd 'strats'    # Slack 명령을 로컬에서
.venv/bin/pytest
```

## 내장 전략과 현재 백테스트 결과 (미국)

2021-10 ~ 2026-11, 풀 1,605종목(S&P 500 + NASDAQ·NYSE 시총 상위 각 800), 그날 기준 20일 평균 거래대금
$20M 이상(시점 기준 필터), 비용 왕복 약 0.25%(수수료 0.1%×2, 슬리피지 0.05%×2, SEC fee), 동시 5종목.
표본 외(OOS) = 최근 25% 구간.

| 전략 | 출처 (근거 수준) | 거래 | 승률 | 거래당 기대값 | PF | 표본 외 PF |
|---|---|---|---|---|---|---|
| **earnings_gap_drift** | PEAD·실적 모멘텀 (학술, 비용 후 생존 확인) | 178 | 36% | **+3.75%** | **1.45** | 1.57 |
| **qullamaggie_breakout** | Qullamaggie 본인 블로그 (실계좌 미검증) | 519 | 37% | +1.49% | 1.34 | 1.96 |
| **minervini_vcp** | Minervini VCP (USIC 우승, 주최측 검증) | 146 | 33% | +2.33% | 1.62 | 3.18 |
| **bb_trend_pullback** | Bollinger + 추세 내 눌림목 (이 시스템의 데이터 탐색) | 729 | 59% | +0.55% | 1.22 | 1.70 |
| donchian_turtle (상승장만) | 터틀 System 2 | 164 | 30% | +1.43% | 1.23 | 2.66 |
| volume_thrust | 거래량 급증 장대양봉 | 562 | 42% | +0.25% | 1.07 | 1.33 |
| high52_momentum | 52주 신고가 (학술) | 320 | 33% | +0.28% | 1.06 | 1.19 |
| squeeze_breakout | Bollinger 스퀴즈 | 516 | 37% | −0.55% | 0.79 | 0.69 |
| rsi2_reversion | Connors RSI(2) (공개 후 우위 약화 보고) | 1,304 | 54% | −0.51% | 0.75 | 0.73 |
| volatility_breakout | Larry Williams | 5,329 | 41% | −0.34% | 0.72 | 0.84 |

주변 파라미터 민감도: qullamaggie_breakout 6/6, earnings_gap_drift(60일 보유) 6/6, bb_trend_pullback 9/10 설정이
구간 내·외 모두 PF > 1. minervini_vcp는 구간 내 PF 0.89로 최근 강세장 의존.

**워크포워드 모의투자** (`stockmining simulate`, 2024-Q2~2026-11): 전략 전부 같은 비중 + 동시 10종목이 가장
안정적이었습니다(연 40%, MDD 24%, 1년 수익 확률 96%). 다만 신규 전략 3개는 전체 기간 데이터를 본 뒤 고른 것이라
이 숫자에는 데이터 스누핑이 섞여 있습니다. 무작위 선택 기준선은 연 −3% 안팎입니다.

**해석**
- 한국장보다 낫습니다. 거래세가 없어 비용이 절반 수준이고, 추세 계열이 플러스로 돌아섭니다.
- **bb_trend_pullback**이 가장 믿을 만합니다. 손절 2~4ATR·청산 10/20일선 등 주변 설정 10개 중 9개가
  구간 내·외 모두 PF > 1이었습니다(우연히 맞은 한 점이 아님). 그래도 거래당 +0.5% 수준의 얇은 우위입니다.
- 터틀은 기대값은 크지만 이익이 최근 강세장에 몰려 있습니다(구간 내 PF 0.83).
- 학습기는 현재 국면에서 기대값이 양수인 전략에만 가중치를 주고, 페이퍼 트레이딩 결과로 계속 갱신합니다.

## 스스로 수익 구조를 찾는 방법

| 주기 | 무엇을 | 결과물 |
|---|---|---|
| 매일 | 모든 전략 신호를 페이퍼 트레이딩, 학습기가 국면별 가중치 갱신 | 추천, 전략 음소거·재활성화 |
| 매일 | Claude가 그날 데이터·엣지 맵·열린 가설을 보고 **학습 일지**에 관찰·가설·교훈을 남기고, 기존 가설을 확인/기각 | `/insights` |
| 매주 | **엣지 맵**: 전략별 매매를 진입 시점 조건(변동성, 상대강도, 거래량, 이격도, 단기 수익률, 국면)으로 3등분해 어디서 벌고 잃는지 통계 검정(t ≥ 2) | `/edge` |
| 매주 | 엣지 맵의 강한 발견 → 진입 필터로 변환 → 백테스트 게이트 + 기존 규칙 대비 개선(구간 내 +0.1%p, 표본 외 유지) 확인 → 승인 요청 | `/approve` |
| 매주 | 파라미터 워크포워드 튜닝(자동 반영), Claude 웹 리서치(열린 가설도 검증 대상으로) | 새 규칙 후보 |

예) 첫 엣지 맵에서 발견된 공통 구조: **변동성 하위 1/3(ATR% 약 2.5 미만) 종목에서는 10개 전략 중 8개가 손실**.

## 투자 성향 (`profile`)

| | 안정형 (`profile: normal` + `selection: equal`, `max_positions: 10`) | 기본형 (`normal`) | 공격형 (`aggressive`) |
|---|---|---|---|
| 동시 보유 / 1회 손실 한도 | 10종목 / 1% | 5종목 / 1% | **3종목 / 2%**, 종목당 최대 40% |
| 전략 | 전체 균등 | 학습기 가중 | Qullamaggie·Minervini VCP·실적 갭·터틀만 균등 |
| 워크포워드 CAGR / MDD | 40% / 24% | 19% / 28% | 109% / 26% |
| 최악의 달 / 월 승률 | −5% / 68% | −15% / 61% | −8% / **45%** |
| 100만 원 1년 중앙값 (우위 절반 할인) | 118만 | 108만 | 131만 |
| 같은 조건 하위 5% / 원금 손실 확률 | 85만 / 21% | 70만 / 39% | 66만 / 28% |
| 3년 내 30% 이상 낙폭 확률 (할인) | 9% | 48% | **70%** |

공격형의 고수익은 소수 대박 종목에서 나옵니다. 68건 중 상위 5건(MRVL +260%, LUMN +139%, LITE +126% 등)이
전체 이익의 82%였습니다. 그 5건을 빼도 거래당 +2.45%, PF 1.42로 플러스지만, 대박이 한 번도 안 걸리는 해에는
안정형보다 못할 수 있습니다. 표본(68건)이 작고, 포함된 전략 일부는 전체 기간 데이터를 보고 고른 것입니다.
레버리지·신용은 쓰지 않습니다. `stockmining simulate --profiles`로 언제든 다시 계산할 수 있습니다.

## 한계 (꼭 읽어 주세요)

- **생존 편향이 한국보다 큽니다.** 풀이 "오늘의" S&P 500과 시총 상위 종목이라 과거의 승자가 미리 들어가 있습니다.
  특히 모멘텀·돌파 전략 성과가 부풀려집니다. 페이퍼 트레이딩 실적이 쌓이기 전까지는 백테스트를 상한으로 보세요.
- 실적 발표 데이터가 없어 백테스트는 실적 갭을 피하지 못합니다. 실전에서는 개장 전 브리핑이 실적일을 검색해 보류시킵니다.
- 세금: 해외주식 양도차익은 연 250만 원 공제 후 22%. 백테스트 수익률은 세전입니다.
- 환율: 원화 기준 수익은 달러 환율 변동에 따라 달라집니다(환전 스프레드는 매매마다가 아니라 환전할 때 한 번).
- 포트폴리오 MDD는 청산 시점 기준이라 실제보다 약간 작게 나옵니다.
- 이 시스템은 의사결정 보조 도구입니다. 수익을 보장하지 않습니다.

<details><summary>한국장(market: kr) 결과 참고</summary>

같은 전략을 KRX 전 보통주에 돌리면 비용(왕복 0.43%, 거래세 0.20%) 때문에 상승장 한정 Minervini(PF 1.38)를 빼고
전부 마이너스였습니다. pykrx는 KRX 로그인 의무화로 불안정해 FinanceDataReader(네이버)를 씁니다.
</details>

## 구조

```
src/stockmining/
  data.py       시세 수집 (FinanceDataReader: 미국=Yahoo, 한국=네이버), 유니버스
  features.py   지표 + 안전한 식 언어 (Claude 규칙도 이 언어로만)
  rules.py      규칙 DSL, 진입/청산 시뮬레이션
  backtest.py   종목별 시뮬 → 포트폴리오 → 지표, 워크포워드 튜닝, 게이트
  engine.py     일일 신호, 페이퍼 트레이딩, 추천·수량 계산
  learner.py    국면별 베이지안 가중치, 음소거/재활성화
  journal.py    매매일지, 포지션, 원칙 위반 점검
  analyst.py    Claude (저녁 리뷰, 아침 웹 브리핑, 주간 리서치, /ask)
  jobs.py       배치 오케스트레이션, 제안 승인
  bot.py        Slack(기본)·텔레그램 봇
strategies/*.yaml   내장 전략 (출처 포함)
principles.md       초기 원칙 (미국 스윙용. 운영 중에는 data/principles.md 가 갱신됨)
deploy/             Terraform, systemd, 배포 스크립트
```
