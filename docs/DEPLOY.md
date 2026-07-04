# 배포 가이드 (Phase 3)

읽기 위주 트래픽 + SQLite 단일 파일 구조라 단일 인스턴스로 충분하다.

## 공통 준비

1. 데이터 스냅샷 확보 — GitHub Actions의 `pwc-sqlite` 아티팩트 다운로드
   (매일 update-data 워크플로가 갱신) → `data/pwc.sqlite`
2. 컨테이너 빌드/실행:

```bash
docker build -t paper-with-me .
docker run -p 8000:8000 -v $(pwd)/data:/data paper-with-me
```

## 호스팅 옵션 비교

| 옵션 | 비용 | 장점 | 단점 |
|---|---|---|---|
| Hugging Face Spaces (Docker) | 무료(CPU basic) | PWC의 정신적 후속지, 무료, HF 생태계 노출 | 영구 스토리지는 유료, 콜드 스타트 |
| Fly.io / Render / Railway | 소액 (~$5+/월) | 볼륨 지원, 커스텀 도메인 용이 | 계정/결제 필요 |
| 자체 VPS (EC2/Lightsail 등) | ~$5+/월 | 완전한 통제 | 직접 운영 부담 |

### 데이터 크기 주의

스냅샷 원본은 ~13GB(zip 3.2GB)다. 저장 공간이 작은 무료 호스팅에서는
`VACUUM` 후 배포하거나 데이터 일부(sota/papers만)로 줄이는 방법을 쓴다:

```bash
sqlite3 data/pwc.sqlite "VACUUM;"
```

### 자동 배포 (호스트 결정 후)

호스트가 정해지면 `.github/workflows/deploy.yml`을 추가해
`update-data` 완료 시 스냅샷을 배포 대상에 동기화하는 파이프라인을 붙인다.
