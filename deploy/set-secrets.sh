#!/bin/bash
# 토큰 4개를 하나씩 안내하며 입력받아, 형식·실제 동작을 확인한 뒤에만 AWS SSM에 저장합니다.
# 사용: 별도 터미널에서  ./deploy/set-secrets.sh
set -uo pipefail
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"

ask() {  # ask <이름> <설명> <정규식> → 형식이 맞을 때까지 다시 물음
  local name="$1" help="$2" re="$3" v
  while true; do
    echo
    echo "▶ $name"
    echo "  $help"
    printf "  붙여넣기 후 Enter (입력은 화면에 *로만 보입니다; 영문 입력 상태 확인): "
    v=""
    while IFS= read -rs -n1 c; do
      [[ -z "$c" ]] && break
      if [[ "$c" == $'\x7f' ]]; then [[ -n "$v" ]] && { v="${v%?}"; printf '\b \b'; }; continue; fi
      v+="$c"; printf '*'
    done
    echo
    v="$(printf '%s' "$v" | tr -d '[:space:]')"
    if [[ "$v" =~ $re ]]; then REPLY="$v"; return 0; fi
    echo "  ✗ 형식이 맞지 않습니다 (길이 ${#v}, 시작 '${v:0:5}'). 다시 입력해 주세요."
  done
}

put() { aws ssm put-parameter --overwrite --type SecureString --name "/stockmining/$1" --value "$2" >/dev/null \
          && echo "  ✓ /stockmining/$1 저장" || { echo "  ✗ 저장 실패"; exit 1; }; }

echo "=== Stock Mining 토큰 등록 (리전 $AWS_REGION) ==="

ask "1/4 슬랙 봇 토큰" "api.slack.com/apps → 내 앱 → OAuth & Permissions → 'Bot User OAuth Token' (xoxb-로 시작)" '^xoxb-[A-Za-z0-9-]{20,}$'
BOT="$REPLY"
ok=$(curl -s -H "Authorization: Bearer $BOT" https://slack.com/api/auth.test)
if [[ "$ok" != *'"ok":true'* ]]; then echo "  ✗ 슬랙이 이 토큰을 거부했습니다: $ok"; exit 1; fi
echo "  ✓ 슬랙 인증 확인"; put SLACK_BOT_TOKEN "$BOT"

ask "2/4 슬랙 앱 토큰 (Access token)" "같은 앱 → Basic Information → App-Level Tokens → 만든 토큰의 Access token (xapp- 또는 xoxe.xapp-로 시작)" '^(xoxe\.)?xapp-[A-Za-z0-9-]{20,}$'
APP="$REPLY"
ok=$(curl -s -X POST -H "Authorization: Bearer $APP" https://slack.com/api/apps.connections.open)
if [[ "$ok" != *'"ok":true'* ]]; then echo "  ✗ 앱 토큰 확인 실패 (Socket Mode 켜짐·connections:write 확인): $ok"; exit 1; fi
echo "  ✓ Socket Mode 확인"; put SLACK_APP_TOKEN "$APP"
if [[ "$APP" == xoxe.* ]]; then
  echo
  echo "  이 앱 토큰은 일정 시간 뒤 만료되는 방식이라, 서버가 자동으로 갱신할 수 있게 값 3개가 더 필요합니다."
  ask "2-1 Refresh token" "토큰을 만들 때 같이 나온 Refresh token (xoxe-로 시작)" '^xoxe-[A-Za-z0-9-]{20,}$'
  put SLACK_APP_REFRESH_TOKEN "$REPLY"
  ask "2-2 Client ID" "Basic Information → App Credentials → Client ID (숫자.숫자)" '^[0-9]+\.[0-9]+$'
  put SLACK_CLIENT_ID "$REPLY"
  ask "2-3 Client Secret" "같은 곳 Client Secret 칸의 Show 또는 복사 버튼 (영문·숫자 32자)" '^[0-9a-f]{32}$'
  put SLACK_CLIENT_SECRET "$REPLY"
fi

ask "3/4 내 슬랙 멤버 ID" "슬랙에서 내 프로필 사진 → 프로필 → ⋮ (더보기) → '멤버 ID 복사' (U로 시작)" '^[UW][A-Z0-9]{6,}$'
UID_="$REPLY"
ok=$(curl -s -H "Authorization: Bearer $BOT" "https://slack.com/api/conversations.open?users=$UID_")
if [[ "$ok" != *'"ok":true'* ]]; then echo "  ✗ 이 ID로 DM을 열 수 없습니다: $ok"; exit 1; fi
ch=$(printf '%s' "$ok" | sed -n 's/.*"channel":{"id":"\([A-Z0-9]*\)".*/\1/p')
curl -s -H "Authorization: Bearer $BOT" -H 'Content-type: application/json; charset=utf-8' \
  -d "{\"channel\":\"$ch\",\"text\":\"✅ Stock Mining 봇 연결 완료. 서버 배포 후 리포트가 여기로 옵니다.\"}" \
  https://slack.com/api/chat.postMessage >/dev/null
echo "  ✓ 슬랙 DM으로 테스트 메시지를 보냈습니다 (봇 DM을 확인해 보세요)"; put SLACK_USER_ID "$UID_"

echo
echo "  Claude 토큰은 새 터미널 탭에서 'claude setup-token' 을 실행하면 브라우저 로그인 후 sk-ant-... 로 나옵니다."
ask "4/4 Claude 구독 토큰" "claude setup-token 결과 (sk-ant-로 시작하는 긴 문자열)" '^sk-ant-[A-Za-z0-9_-]{40,}$'
put CLAUDE_CODE_OAUTH_TOKEN "$REPLY"

echo
echo "=== 완료. Claude Code에 '완료'라고 알려주세요. ==="
