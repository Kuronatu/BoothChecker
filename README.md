# BoothChecker

[![latest](https://github.com/MAX-FLAVOR/BoothChecker/actions/workflows/latest-build.yml/badge.svg)](https://github.com/MAX-FLAVOR/BoothChecker/actions/workflows/latest-build.yml)
[![develop](https://github.com/MAX-FLAVOR/BoothChecker/actions/workflows/develop-build.yml/badge.svg)](https://github.com/MAX-FLAVOR/BoothChecker/actions/workflows/develop-build.yml)

BOOTH.pm의 아이템 업데이트를 주기적으로 확인하고 업데이트 감지 시 Discord Bot으로 메세지를 전송합니다.

### 주간 의존성 업데이트

[`weekly dependency rebuild`](.github/workflows/weekly-build.yml)는 매주 월요일 09:00 KST에 기본 브랜치의 코드로 `booth-checker`, `booth-discord` 이미지를 다시 빌드하고 GHCR의 `latest` 태그로 게시합니다. 베이스 이미지를 새로 내려받고 빌드 캐시를 사용하지 않아, Dockerfile의 `apt-get upgrade`와 버전이 고정되지 않은 `requirements.txt` 설치가 최신 패키지를 반영합니다. 의존성 파일 자체는 수정하지 않습니다.

워크플로 파일이 기본 브랜치에 반영되어야 예약 실행이 활성화됩니다. GitHub Actions의 **Run workflow**에서 기본 브랜치를 선택하면 수동 실행할 수도 있습니다. 게시된 이미지를 실행 중인 서비스에 반영하려면 `docker compose pull && docker compose up -d`를 실행하세요.

GitHub 예약 실행은 지연될 수 있으며, 공개 저장소는 60일간 활동이 없으면 예약 실행이 자동으로 비활성화됩니다.

---

### config.json

```
{
    "refresh_interval": 600,
    "selenium_url": "http://chrome:4444/wd/hub",
    "postgres": {
        "host": "postgres",
        "port": 5432,
        "dbname": "booth",
        "user": "booth_user",
        "password": "booth_password"
    },
    "discord_api_url": "http://booth-discord:5000",
    "discord_bot_token": "YOUR_DISCORD_BOT_TOKEN",
    "gemini_api_key": "YOUR_GEMINI_API_KEY",
    "gemini_model": "gemini-3.5-flash-lite",
    "s3":
        {
            "endpoint_url": "YOUR_S3_ENDPOINT_URL",
            "bucket_name": "YOUR_S3_BUCKET_NAME",
            "bucket_access_url": "YOUR_S3_BUCKET_ACCESS_URL",
            "access_key_id": "YOUR_S3_ACCESS_KEY_ID",
            "secret_access_key": "YOUR_S3_SECRET_ACCESS_KEY"
        }
}
```

#### `s3` (선택사항)

changelog.html을 S3에 업로드하고, Discord Embed에서 마스킹된 링크로 제공합니다.

`s3`를 사용하지 않을 경우, `s3` 부분을 제거하면 됩니다. 이 경우 changelog.html은 Discord에 직접 업로드됩니다.

#### 'gemini_api_key' (선택사항)

변경점을 Google Gemini를 통해 요약합니다.

#### 'gemini_model' (선택사항)

요약에 사용할 Gemini 모델을 지정합니다. 생략하면 기본값 `gemini-3.5-flash-lite`를 사용합니다.

---

### Font
`JetBrains Mono`

https://www.jetbrains.com/lp/mono/

`Google Noto`

https://fonts.google.com/noto
