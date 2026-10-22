# BoothChecker

[![Build and publish images](https://github.com/Kuronatu/BoothChecker/actions/workflows/build-images.yml/badge.svg)](https://github.com/Kuronatu/BoothChecker/actions/workflows/build-images.yml)

BOOTH에서 구매하거나 선물로 받은 다운로드 상품의 업데이트를 확인하고, 파일 변경 내역을 Discord로 보내는 셀프 호스팅 봇입니다.

ZIP과 UnityPackage 내부의 파일 추가·삭제·변경을 비교해 HTML 변경 내역을 만듭니다. FBX만 추적하거나, Gemini로 변경 목록을 한국어로 요약하거나, S3 호환 스토리지에 변경 내역을 게시할 수 있습니다.

## 시작하기

Docker와 Docker Compose, Discord 봇 토큰, 추적할 상품에 접근할 수 있는 BOOTH 계정이 필요합니다. 기본 구성은 Gemini와 S3 없이 동작합니다.

### 1. Discord 봇 준비

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 애플리케이션을 만들고 봇 토큰을 발급합니다.
2. Bot 설정에서 **Message Content Intent**를 활성화합니다. 현재 코드가 이 인텐트를 요청하므로 포털에서도 허용해야 합니다. [Discord 인텐트 안내](https://github.com/discord/discord-api-docs/blob/main/developers/events/gateway.mdx)
3. `bot`, `applications.commands` 범위로 봇을 서버에 설치합니다. [Discord 설치 안내](https://github.com/discord/discord-api-docs/blob/main/developers/quick-start/getting-started.mdx)
4. 알림 채널에서 채널 보기, 메시지 보내기, 링크 임베드, 파일 첨부 권한을 부여합니다. 업데이트 메시지에는 `@here`가 포함되므로 멘션 권한도 운영 목적에 맞게 설정합니다.

### 2. 저장소와 설정 준비

```bash
git clone https://github.com/Kuronatu/BoothChecker.git
cd BoothChecker
mkdir -p version archive changelog
```

저장소 루트에 다음 내용으로 `config.json`을 만듭니다. 이미 운영 중이라면 기존 설정을 덮어쓰지 말고 필요한 항목만 반영하세요.

```json
{
  "refresh_interval": 600,
  "fbx_only": false,
  "selenium_url": "http://chrome:4444/wd/hub",
  "postgres": {
    "host": "postgres",
    "port": 5432,
    "dbname": "booth",
    "user": "booth_user",
    "password": "CHANGE_ME"
  },
  "discord_api_url": "http://booth-discord:5000",
  "discord_bot_token": "YOUR_DISCORD_BOT_TOKEN"
}
```

`discord_bot_token`을 실제 토큰으로 바꾸고, `postgres.password`와 [docker-compose.yml](docker-compose.yml)의 `POSTGRES_PASSWORD`를 **같은 비밀번호**로 설정합니다. 위 예시는 새 데이터베이스를 만드는 경우를 기준으로 합니다. 기존 데이터베이스의 비밀번호는 설정 파일만 바꿔도 변경되지는 않습니다.

**`fbx_only`는 필수입니다.** 현재 [config-sample.json](config-sample.json)에는 이 항목이 빠져 있으므로 샘플을 복사했다면 직접 추가해야 합니다. 샘플의 Gemini·S3 자리표시자도 실제 값으로 바꾸거나 해당 항목을 제거하세요.

기본 Compose는 PostgreSQL을 호스트의 `5433` 포트에 공개합니다. 컨테이너끼리만 접속한다면 `postgres`의 `ports` 항목을 제거하세요. `config.json`의 DB 포트는 컨테이너 내부 포트인 `5432`를 유지합니다.

### 3. 실행

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs -f booth-discord booth-checker
```

`booth-discord` 로그의 `Logged in as`, `Synced 6 command(s)`와 `booth-checker` 로그의 `BoothChecker cycle started`를 확인합니다. 등록된 상품이 없으면 `Found 0 items to check.`가 표시됩니다.

### 4. 계정과 상품 등록

BOOTH에 로그인한 브라우저의 개발자 도구에서 BOOTH 쿠키 `_plaza_session_nktz7u`의 **값만** 복사합니다. Discord에서 아래 슬래시 명령어를 차례로 실행하세요.

```text
/booth cookie:<BOOTH 세션 쿠키 값>
/item_add item_number:<상품 번호>
/item_list
```

상품 번호는 `https://booth.pm/ko/items/1234567`의 `1234567` 부분입니다. 상품을 등록한 채널로 알림이 전송되며, 다음 검사 사이클부터 확인합니다. 최초 검사에서는 비교 기준을 저장하고 새 아이템 알림을 보냅니다. 단, FBX 전용 모드에서 FBX가 없으면 알림을 생략합니다.

**BOOTH 세션 쿠키는 계정 접근에 사용되는 비밀값입니다.** 명령어 응답은 실행자에게만 보이지만 쿠키는 서버의 PostgreSQL에 평문으로 저장됩니다. 신뢰하는 서버에서 운영하고, 설정 파일과 DB 백업을 공개하지 마세요. `config.json`은 Git 추적에서 제외되어 있습니다.

## Discord 명령어

### 계정

`/booth cookie:<값>`은 BOOTH 계정을 등록하거나 만료된 쿠키를 교체합니다. 등록 즉시 쿠키의 유효성을 검증하지는 않습니다.

`/booth_del`은 계정 등록을 해제합니다. 먼저 `/item_del`로 본인의 등록 상품을 모두 삭제해야 합니다.

### 상품

`/item_add item_number:<상품 번호>`는 상품과 현재 알림 채널을 등록합니다. 구매·기프트 주문 정보를 자동으로 찾으며, 다음 옵션을 지원합니다.

- `item_name`: 알림에 표시할 이름. 생략하면 BOOTH 상품명을 사용합니다.
- `order_number`: 자동 조회가 어려운 경우 지정하는 구매 주문 번호. 수동 입력은 일반 구매로 처리되므로 기프트 주문에는 사용하지 마세요.
- `intent_encoding`: ZIP 파일명 해석에 사용할 인코딩. 기본값은 `shift_jis`입니다.
- `summary_this`: 해당 상품의 Gemini 요약 사용 여부. 기본값은 `true`이며 전역 Gemini 설정도 필요합니다.
- `fbx_only`: 해당 상품에서 FBX 변경만 알릴지 여부. 기본값은 `config.json`의 `fbx_only`입니다.

`/item_list`는 **현재 채널에 연결된 본인 상품**을 표시합니다.

`/item_del item:<상품 번호>`는 본인의 상품과 연결된 알림 채널 정보를 삭제합니다.

`/noti_update item_number:<상품 번호>`는 해당 상품의 알림 채널을 **명령어를 실행한 채널로 변경**합니다.

상품별 옵션을 수정하는 별도 명령어는 없습니다. 다른 옵션으로 등록하려면 상품을 삭제한 뒤 다시 등록하세요. 이때 기존 `version/json` 비교 기록은 자동으로 삭제되지 않습니다.

## 업데이트 감지 방식

1. 등록된 주문·기프트 페이지에서 다운로드 ID 목록을 가져옵니다.
2. 이전 목록과 비교하고, ID 목록이 달라진 상품의 파일을 다운로드합니다. 목록의 순서만 바뀐 경우는 무시합니다.
3. ZIP·UnityPackage를 풀어 파일 해시를 비교하고, 변경 내역을 HTML로 만듭니다.
4. Discord에 알림과 변경 내역을 전달하고 비교 기준을 갱신합니다.

**상품 설명 수정이나 다운로드 ID가 유지되는 파일 교체는 감지하지 못합니다.** 파일 내용 비교는 다운로드 ID 목록이 바뀌었을 때만 수행합니다.

FBX 전용 모드는 압축 파일 내부를 포함해 FBX의 추가·삭제·해시 변경을 비교합니다. 다운로드 목록이 바뀌어도 FBX 차이가 없으면 알림 없이 비교 기준만 갱신합니다. 전역 `fbx_only` 변경은 새 상품 등록의 기본값에만 적용되며 기존 상품 설정은 바뀌지 않습니다.

### 실패 시 동작

- **일시적인 알림 실패:** 네트워크 오류나 서버 오류 등은 비교 기준을 갱신하지 않고 다음 사이클에 재시도합니다. 전송은 성공했으나 응답만 유실된 경우 중복 알림이 발생할 수 있습니다.
- **영구적인 알림 실패:** 채널 삭제·권한 부족 등으로 내부 API가 `400` 또는 `422`를 반환하면 알림 없이 비교 기준을 갱신합니다. 원인을 고쳐도 그 업데이트가 자동 재전송되지는 않습니다.
- **파일 분석 실패:** 같은 다운로드 목록의 분석이 연속 **3회** 실패하면 경고 문구와 함께 변경 내역 없이 알립니다. 실패 횟수는 메모리에 있어 프로세스를 재시작하면 초기화됩니다.
- **HTML 첨부 실패:** 본문 알림이 성공했다면 비교 기준을 갱신합니다. 첨부 실패만으로 본문을 재전송하지 않습니다.

## 설정

두 서비스는 시작할 때 루트의 `config.json`을 읽습니다. 설정 수정 후 적용하려면 다음 명령을 실행합니다.

```bash
docker compose restart booth-discord booth-checker
```

### 검사 주기와 처리량

- `refresh_interval` **필수**, 초 단위: 전체 검사 사이클이 끝난 뒤 대기하는 시간입니다. `600`이면 처리 시간에 더해 10분을 기다립니다. 양의 정수를 사용하세요.
- `fbx_only` **필수**, 불리언: 새 상품 등록 시 적용할 FBX 전용 모드 기본값입니다.
- `max_workers` 선택, 기본값 `2`: 동시에 검사할 상품 수입니다. `1` 이상의 정수를 사용하세요. 늘리면 다운로드·메모리 사용량과 BOOTH 요청량도 증가합니다.
- `archive_retention_days` 선택, 기본값 `30`: 매 사이클 시작 시 보관 폴더의 수정 시각을 기준으로 오래된 아카이브를 삭제합니다. `0` 이하는 자동 삭제를 끕니다.

`selenium_url`, `postgres`, `discord_api_url`, `discord_bot_token`은 필수 연결 설정입니다. 기본 Compose에서는 시작하기의 서비스 주소를 그대로 사용합니다.

### Gemini 요약, 선택 사항

`config.json` 최상위에 다음 항목을 추가합니다.

```json
{
  "gemini_api_key": "YOUR_GEMINI_API_KEY",
  "gemini_model": "YOUR_GEMINI_MODEL_ID"
}
```

API 키와 유효한 모델 이름을 **둘 다** 지정해야 합니다. 코드에 기본 모델은 없으며, 모델을 생략하거나 `null`, 빈 문자열, 공백만 있는 문자열, 문자열이 아닌 값으로 지정하면 요약을 비활성화합니다. 모델 이름 앞뒤 공백은 제거합니다.

상품의 `summary_this`가 `true`이고 변경 목록이 있을 때 파일명·경로·변경 상태를 Gemini로 보냅니다. FBX 모드에서는 해시 정보도 포함될 수 있습니다. 파일 본문을 보내지는 않습니다. 요약 호출 실패나 빈 응답은 요약 없이 알림을 계속하는 방식으로 처리합니다.

### S3 호환 스토리지, 선택 사항

`config.json` 최상위에 `s3` 객체를 추가합니다.

```json
{
  "s3": {
    "endpoint_url": "https://YOUR_S3_ENDPOINT",
    "bucket_name": "YOUR_BUCKET_NAME",
    "bucket_access_url": "changelog.example.com",
    "access_key_id": "YOUR_ACCESS_KEY_ID",
    "secret_access_key": "YOUR_SECRET_ACCESS_KEY"
  }
}
```

HTML은 `changelog/<UUID>.html` 경로에 업로드되며 Discord 알림에 링크가 표시됩니다. `bucket_access_url`은 **`https://`와 끝의 `/` 없이** 입력합니다. 코드는 `https://<bucket_access_url>/changelog/<UUID>.html`을 만들므로 해당 주소에서 HTML을 읽을 수 있도록 스토리지 접근을 설정해야 합니다. 서명 URL은 생성하지 않습니다.

S3를 사용하지 않으면 `s3` 항목을 제거하세요. 이 경우 HTML을 Discord에 직접 첨부합니다. **S3를 설정한 상태에서 업로드가 실패하면 Discord 첨부로 자동 전환하지 않습니다.** S3 객체의 보관 기간도 `archive_retention_days`와 별개이므로 스토리지에서 관리해야 합니다.

### Syslog, 선택 사항

콘솔 로그와 함께 외부 Syslog 서버로 로그를 보내려면 다음 항목을 추가합니다. 기본 포트는 UDP `514`입니다.

```json
{
  "logging": {
    "syslog": {
      "enabled": true,
      "address": "syslog.example.com",
      "port": 514
    }
  }
}
```

### Dry run

`"dry_run": true`로 설정하면 검사기는 Discord 전송, Gemini 호출, S3 업로드, 버전 JSON 갱신을 생략합니다. 기본값은 `false`입니다.

**읽기 전용 실행은 아닙니다.** BOOTH 요청, 다운로드, 압축 해제, 로컬 HTML 생성, 설정된 아카이브 복사와 오래된 아카이브 삭제는 수행합니다. 운영 데이터와 분리된 환경에서 사용하세요. Discord 봇 자체의 동작을 중지하는 옵션도 아닙니다.

## 운영과 데이터 보관

| 서비스 | 역할 |
| --- | --- |
| `booth-checker` | 주기적 검사, 다운로드, 파일 비교, 변경 내역 생성 |
| `booth-discord` | 슬래시 명령어, Discord 전송, 내부 HTTP API |
| `postgres` | BOOTH 쿠키, 상품 설정, 알림 채널 저장 |
| `chrome` | Selenium을 통한 주문·기프트 정보 조회 |

PostgreSQL 데이터는 Compose의 `postgres-data` 볼륨에 저장됩니다. 호스트의 `version/`에는 주문별 비교 기준 JSON, `archive/`에는 보관 대상 다운로드, `changelog/`에는 전송용 HTML이 저장됩니다. 검사기의 `work/`는 임시 작업 공간이며 상품 처리 후 정리됩니다.

HTML은 일반 실행에서 전송 시도 후 정리되므로 `changelog/`를 영구 기록 저장소로 사용하지 마세요. 상품 등록 시 다운로드 보관 옵션인 `archive_this`는 `false`이며, 이를 켜는 슬래시 명령어는 없습니다. `archive_retention_days`만 지정해도 다운로드 보관이 활성화되지는 않습니다.

**백업에는 PostgreSQL과 `version/`을 함께 포함하세요.** 쿠키·상품 설정과 파일 비교 기준이 서로 다른 곳에 저장됩니다. 필요한 경우 `archive/`도 보관합니다. `docker compose down`은 DB 볼륨을 유지하지만 **`docker compose down -v`는 DB 볼륨을 삭제**합니다.

### 업데이트와 종료

```bash
# 게시된 최신 이미지 적용
docker compose pull
docker compose up -d

# 최근 로그 확인
docker compose logs --tail=100 booth-checker booth-discord

# 컨테이너 종료와 제거, DB 볼륨 유지
docker compose down
```

## 문제 해결

- **`KeyError: 'fbx_only'`:** `config.json` 최상위에 `"fbx_only": false`를 추가하고 두 서비스를 재시작합니다.
- **봇 로그인·명령어 동기화 실패:** 토큰, Message Content Intent, 서버 설치 범위를 확인하고 `booth-discord` 로그를 봅니다.
- **계정 등록 후 상품 등록·검사 실패:** BOOTH에 다시 로그인해 쿠키를 `/booth`로 갱신합니다. 구매·기프트 수령 여부와 Selenium 연결도 확인하세요. BOOTH 페이지 구조 변경도 원인이 될 수 있습니다.
- **알림이 오지 않음:** 현재 채널에서 `/item_list`를 확인하고 필요하면 `/noti_update`를 실행합니다. 채널 권한, 검사기 로그, 다운로드 ID 변경 여부와 FBX 전용 모드도 확인하세요.
- **요약이 없음:** `gemini_api_key`, `gemini_model`, 상품의 `summary_this`, `dry_run` 설정을 확인합니다. API 오류나 빈 응답도 요약 생략으로 처리됩니다.
- **변경 내역 링크·첨부가 없음:** 파일 분석 실패 로그와 S3 설정·업로드 로그를 확인합니다. S3 업로드 실패 시 자동 첨부는 없으며, 첨부만 실패한 경우도 자동 재전송하지 않습니다.

## 개발과 검증

런타임 이미지는 Python 3.12 기반입니다. 저장소 루트에서 두 서비스의 의존성을 설치하고 단위 테스트를 실행할 수 있습니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r docker/booth-checker/requirements.txt -r docker/booth-discord/requirements.txt
python -m unittest discover -s tests -v
```

테스트는 크롤링 HTML 처리, FBX 비교, ZIP 파일명 인코딩, Gemini 설정, 알림 실패·재시도 동작을 검증합니다. 실제 BOOTH·Discord·Gemini 연동 검증은 별도 운영 환경이 필요합니다.

로컬 소스를 이미지로 빌드해 실행하려면 다음 명령을 사용합니다. 앞서 설명한 `config.json` 준비가 필요합니다. 개발용 Compose의 `POSTGRES_PASSWORD`도 설정과 일치시켜야 합니다.

```bash
docker compose -f docker-compose-test.yml up -d --build
docker compose -f docker-compose-test.yml logs -f
```

개발용 Compose는 PostgreSQL `5433`, Selenium `4444`, Discord 내부 API `5000`을 호스트에 공개합니다. **내부 API에는 인증이 없으므로 외부에 노출하지 마세요.** 기본 Compose와 서비스·볼륨 이름을 공유하므로 별도 환경 없이 동시에 실행하지 마세요.

`test.sh`는 단위 테스트 스크립트가 아닙니다. 아카이브와 생성된 HTML 등을 삭제하고 개발용 컨테이너를 다시 빌드하므로 운영 디렉터리에서는 실행하지 마세요.

### 코드 위치

- [booth_checker/](booth_checker/): 업데이트 검사와 파일 분석 파이프라인
- [booth_discord/](booth_discord/): 봇 명령어, 주문 조회, 알림 API와 DB 등록 처리
- [templates/changelog.html](templates/changelog.html): 변경 내역 HTML 템플릿
- [tests/](tests/): 단위·회귀 테스트
- [docker/](docker/): 서비스별 Dockerfile과 의존성
- [.github/workflows/build-images.yml](.github/workflows/build-images.yml): 이미지 빌드·게시 워크플로

## 이미지 빌드와 배포

GitHub Actions는 `booth-checker`, `booth-discord` 이미지를 빌드해 각각 `ghcr.io/kuronatu/booth-checker`, `ghcr.io/kuronatu/booth-discord`에 게시합니다.

- **`main` 푸시:** `latest`, `sha-<커밋 SHA 앞 7자리>` 태그를 게시합니다.
- **`main` 대상 PR:** 같은 저장소의 PR은 `pr-<PR 번호>` 태그를 게시하며 `latest`를 갱신하지 않습니다. 외부 포크와 Dependabot PR은 빌드만 수행합니다.
- **예약 실행:** 매주 월요일 **09:00 KST, 00:00 UTC**에 `main` 이미지를 다시 빌드합니다.
- **수동 실행:** Actions의 **Run workflow**에서 `main`을 선택합니다.

예약·수동 실행은 베이스 이미지를 다시 내려받고 빌드 캐시 없이 `apt-get upgrade`와 버전이 고정되지 않은 Python 의존성 설치를 수행합니다. 의존성 파일 자체는 수정하지 않으며 Git 태그나 GitHub Release도 생성하지 않습니다. 실행 중인 서비스에는 자동 적용되지 않으므로 운영자가 `docker compose pull`과 `docker compose up -d`를 실행해야 합니다.

예약 실행을 사용하려면 워크플로가 기본 브랜치에 있어야 하며, 이 저장소의 작업 조건은 `main`을 대상으로 합니다. GitHub 예약 실행은 지연될 수 있고, 공개 저장소에서는 **60일간 활동이 없으면 자동 비활성화**됩니다. [GitHub 예약 실행 안내](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

## 글꼴

변경 내역 HTML은 Google Fonts에서 [JetBrains Mono](https://www.jetbrains.com/lp/mono/), [Noto Sans JP·KR](https://fonts.google.com/noto)를 불러옵니다.
