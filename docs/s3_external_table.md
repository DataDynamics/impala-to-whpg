# S3 외부 테이블 적재 설정

`load_method: s3` 를 쓰면 Impala 결과를 S3에 gzip 파일로 나눠 올린 뒤, Greenplum
외부 테이블로 읽어 `INSERT ... SELECT` 로 적재합니다. 이 문서는 그 전제 조건인
Greenplum 쪽 설정을 다룹니다.

## 왜 S3를 거치는가

`COPY FROM STDIN` 은 모든 데이터가 마스터 한 대를 통과합니다. 마스터가 병목이 되고,
세그먼트는 마스터가 나눠주는 만큼만 일합니다.

S3 방식은 각 세그먼트가 자기 몫의 S3 오브젝트를 직접 읽습니다. 세그먼트가 16대면
16개의 병렬 읽기가 동시에 일어나고, 마스터는 쿼리 조율만 합니다. 데이터가 커질수록
차이가 벌어집니다.

```
COPY:  Impala → 파이썬 → 마스터 → (재분배) → 세그먼트
S3  :  Impala → 파이썬 → S3 → 세그먼트 N대가 병렬로 직접 읽기
```

대신 S3 왕복이 추가되므로, 수만 건 수준의 소량 데이터는 `load_method: copy` 가
오히려 빠릅니다. 작업별로 골라 쓰면 됩니다.

## 1. 세그먼트에 s3 프로토콜 설정 파일 배포

Greenplum의 `s3` 프로토콜은 **자체 설정 파일** 로 S3에 접근합니다. 파이썬이 쓰는
boto3 자격증명과는 별개이고, 이 파일은 마스터와 모든 세그먼트 호스트의 **동일한
경로** 에 있어야 합니다.

파일 내용은 아래 헬퍼로 만들 수 있습니다.

```python
from impala_to_greenplum import render_gp_s3_config

print(render_gp_s3_config("AKIA...", "secret...", section="default"))
```

```ini
[default]
accessid = AKIA...
secret = secret...
threadnum = 4
chunksize = 67108864
encryption = true
version = 1
```

배포와 권한 설정:

```bash
# 마스터에서 작성한 뒤 전 세그먼트 호스트로 복사
gpscp -f /home/gpadmin/hostfile /home/gpadmin/s3.conf =:/home/gpadmin/s3.conf
gpssh -f /home/gpadmin/hostfile -e 'chmod 600 /home/gpadmin/s3.conf'
```

자격증명이 평문으로 들어가므로 권한은 반드시 600으로 두세요. EC2에서 IAM 역할을
쓴다면 `accessid`/`secret` 을 비우고 인스턴스 프로파일을 사용할 수도 있습니다.

설정이 맞는지는 Greenplum이 제공하는 검증 도구로 확인합니다.

```bash
gpcheckcloud -c "s3://s3.ap-northeast-2.amazonaws.com/dw-stage/ config=/home/gpadmin/s3.conf"
```

## 2. 설정 파일 작성

```yaml
s3:
  bucket: dw-stage
  prefix: impala-to-greenplum
  endpoint: s3.ap-northeast-2.amazonaws.com   # LOCATION에 들어가는 값
  region: ap-northeast-2
  gp_config: /home/gpadmin/s3.conf            # 1번에서 배포한 경로
  file_size_mb: 128
  compress: true
  cleanup: true

jobs:
  - query: SELECT * FROM sales.orders WHERE dt = '2026-08-01'
    target_table: orders
    mode: truncate
    load_method: s3
```

boto3 업로드 자격증명은 `access_key_id` / `secret_access_key` 로 직접 줄 수도 있고,
생략하면 환경변수(`AWS_ACCESS_KEY_ID` 등)나 IAM 역할을 따릅니다.

## 3. 실제로 만들어지는 SQL

파이프라인이 생성하는 외부 테이블은 다음과 같은 형태입니다.

```sql
CREATE READABLE EXTERNAL TEMP TABLE "ext_9f2c1a7b3e5d4088" (
    "order_id" bigint,
    "name" text,
    "amount" numeric(18,2)
)
LOCATION ('s3://s3.ap-northeast-2.amazonaws.com/dw-stage/impala-to-greenplum/orders-9f2c1a7b3e5d4088/ region=ap-northeast-2 config=/home/gpadmin/s3.conf')
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')
ENCODING 'UTF8';

INSERT INTO "staging"."orders" ("order_id", "name", "amount")
SELECT "order_id", "name", "amount" FROM "ext_9f2c1a7b3e5d4088";
```

몇 가지 짚어둘 점이 있습니다.

- 외부 테이블 컬럼 타입은 **대상 테이블의 실제 타입** 을 그대로 따라갑니다. 그래야
  `INSERT ... SELECT` 에서 불필요한 캐스팅이나 타입 불일치가 생기지 않습니다.
- 실행마다 `{prefix}/{테이블명}-{난수}/` 아래에 파일을 올리므로, 같은 버킷에서 여러
  작업이 동시에 돌아도 서로의 파일을 읽지 않습니다.
- 파일 포맷은 COPY TEXT와 동일합니다(탭 구분, `\N` NULL). `.gz` 확장자는 Greenplum이
  보고 알아서 풀어 읽습니다.
- 외부 테이블은 임시 테이블로 만들어 세션이 끝나면 사라지고, 적재 직후 명시적으로
  DROP합니다. `use_temp_external_table: false` 로 두면 `greenplum.schema` 에
  일반 테이블로 만듭니다.

## 4. 파일 개수와 병렬성

**가장 중요한 튜닝 포인트입니다.** Greenplum은 S3 오브젝트를 세그먼트에 나눠
할당하므로, 파일이 하나면 세그먼트 하나만 일합니다.

파일 개수는 `file_size_mb` 로 조절합니다. 총 데이터가 10GB이고 `file_size_mb: 128`
이면 약 80개 파일이 생기므로, 세그먼트가 16대여도 충분히 고르게 퍼집니다. 반대로
총 1GB인데 `file_size_mb: 1024` 로 두면 파일이 하나뿐이라 병렬성이 사라집니다.

파이프라인은 파일 수가 세그먼트 수보다 적으면 경고를 남깁니다.

```
WARNING S3 파일이 3개뿐이라 세그먼트 16개를 다 쓰지 못합니다.
        s3.file_size_mb를 줄이면 파일이 더 잘게 나뉩니다.
```

세그먼트 수는 다음으로 확인합니다.

```sql
SELECT count(*) FROM gp_segment_configuration WHERE content >= 0 AND role = 'p';
```

## 5. 정리와 실패 처리

- `cleanup: true`(기본)면 적재 성공/실패와 무관하게 이 실행이 올린 오브젝트를
  삭제합니다. 접두사 전체가 아니라 **실제로 올린 키만** 지우므로 같은 버킷을 쓰는
  다른 작업의 파일은 건드리지 않습니다.
- 업로드 도중 실패한 경우에도 이미 올라간 파일은 정리 대상에 포함됩니다.
- 디버깅할 때는 `cleanup: false` 로 두고 파일을 직접 열어보세요. 목록을 확인하는
  방법은 [boto3로 S3 파일 목록 보기](boto3.md)에 정리해 두었습니다.
- Greenplum 쪽 적재는 한 트랜잭션이라 실패 시 롤백됩니다. S3 파일만 정리하면
  깨끗한 상태로 되돌아갑니다.

## 6. 형식 오류 허용

원본에 깨진 값이 섞여 있어 일부 행을 버리고 진행하고 싶다면:

```yaml
s3:
  segment_reject_limit: 100
```

`LOG ERRORS SEGMENT REJECT LIMIT 100 ROWS` 가 붙어 100건까지는 오류 행을 건너뜁니다.
버려진 행은 다음으로 확인합니다.

```sql
SELECT * FROM gp_read_error_log('ext_9f2c1a7b3e5d4088');
```

기본값 0은 오류가 하나라도 나면 즉시 실패입니다. 데이터 정합성이 중요하면 기본값을
그대로 두세요.

## PXF를 쓰는 경우

내장 `s3` 프로토콜 대신 PXF가 이미 구축되어 있다면:

```yaml
s3:
  bucket: dw-stage
  protocol: pxf
  pxf_server: s3srv
```

LOCATION이 `pxf://dw-stage/{prefix}/{run}/?PROFILE=s3:text&SERVER=s3srv` 형태로
바뀝니다. `endpoint` 나 `gp_config` 는 필요 없고, 대신 PXF 서버 쪽
`s3-site.xml` 에 자격증명이 설정되어 있어야 합니다.
