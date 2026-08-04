# S3 외부 테이블로 읽기

`bin/query-to-csv` 로 뽑은 파일을 `bin/s3-ops` 로 S3에 올려두면, Greenplum 외부
테이블로 그 파일을 직접 읽을 수 있습니다. 이 문서는 그 전제 조건인 Greenplum 쪽
설정과 외부 테이블 작성법을 다룹니다.

```bash
# 1) Impala에서 뽑고 (접속 정보는 conf/config.yaml 에서 온다)
bin/query-to-csv \
    --query "SELECT * FROM sales.orders WHERE dt = '2026-08-01'" \
    --output orders.csv.gz --delimiter $'\t' --null-string '\N' --no-header --gzip

# 2) S3에 올리고
bin/s3-ops upload orders.csv.gz s3://dw-stage/orders/2026-08-01/

# 3) Greenplum에서 외부 테이블로 읽는다 (아래 3번 절)
```

## 왜 S3를 거치는가

파일을 마스터에 두고 `COPY` 로 밀어넣으면 모든 데이터가 마스터 한 대를 통과합니다.
마스터가 병목이 되고, 세그먼트는 마스터가 나눠주는 만큼만 일합니다.

외부 테이블 방식은 각 세그먼트가 자기 몫의 S3 오브젝트를 직접 읽습니다. 세그먼트가
16대면 16개의 병렬 읽기가 동시에 일어나고, 마스터는 쿼리 조율만 합니다. 데이터가
커질수록 차이가 벌어집니다.

```
COPY:  파일 → 마스터 → (재분배) → 세그먼트
S3  :  파일 → S3 → 세그먼트 N대가 병렬로 직접 읽기
```

대신 S3 왕복이 추가되므로, 수만 건 수준의 소량 데이터는 `psql \copy` 가 오히려
간단하고 빠릅니다.

## 1. 세그먼트에 s3 프로토콜 설정 파일 배포

Greenplum의 `s3` 프로토콜은 **자체 설정 파일** 로 S3에 접근합니다. `bin/s3-ops` 가
쓰는 boto3 자격증명과는 별개이고, 이 파일은 마스터와 모든 세그먼트 호스트의
**동일한 경로** 에 있어야 합니다.

파일은 이런 모양입니다.

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

## 2. 파일을 S3에 올리기

`bin/s3-ops` 로 올립니다. 버킷과 자격증명은 `conf/config.yaml` 에서 자동으로
읽으므로 매번 줄 필요가 없습니다.

```bash
bin/s3-ops upload ./out/ s3://dw-stage/orders/2026-08-01/ --recursive
bin/s3-ops ls s3://dw-stage/orders/2026-08-01/

# 다른 설정 파일을 쓸 때
bin/s3-ops --config conf/config.local.yaml ls s3://dw-stage/orders/2026-08-01/
```

**실행 단위마다 별도 접두사를 쓰세요.** 외부 테이블 LOCATION은 접두사 아래 파일을
전부 읽으므로, 여러 날짜의 파일이 한 디렉터리에 섞이면 의도하지 않은 데이터까지
딸려옵니다. 위 예제처럼 날짜나 실행 ID를 경로에 넣는 편이 안전합니다.

## 3. 외부 테이블 작성

```sql
CREATE READABLE EXTERNAL TABLE ext_orders_20260801 (
    order_id  bigint,
    name      text,
    amount    numeric(18,2)
)
LOCATION ('s3://s3.ap-northeast-2.amazonaws.com/dw-stage/orders/2026-08-01/ region=ap-northeast-2 config=/home/gpadmin/s3.conf')
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')
ENCODING 'UTF8';

INSERT INTO staging.orders (order_id, name, amount)
SELECT order_id, name, amount FROM ext_orders_20260801;

DROP EXTERNAL TABLE ext_orders_20260801;
```

몇 가지 짚어둘 점이 있습니다.

- **`FORMAT` 절은 파일을 만든 옵션과 맞아야 합니다.** `query-to-csv` 의 기본 구분자는
  백틱(`` ` ``)이고 NULL은 빈 문자열입니다. 위 SQL처럼 TEXT 포맷으로 읽으려면 파일을
  뽑을 때 `--delimiter $'\t' --null-string '\N'` 을 주는 편이 편합니다. 헤더 행을
  넣었다면 `--no-header` 로 빼거나, `FORMAT 'CSV' (HEADER)` 로 읽어야 합니다.
- 외부 테이블 컬럼 타입은 **대상 테이블의 실제 타입** 을 그대로 따라가세요. 그래야
  `INSERT ... SELECT` 에서 불필요한 캐스팅이나 타입 불일치가 생기지 않습니다.
- LOCATION의 접두사는 슬래시로 끝내야 그 아래 파일을 모두 읽습니다.
- `.gz` 확장자는 Greenplum이 보고 알아서 풀어 읽습니다. `--gzip` 으로 뽑은 파일을
  그대로 올리면 됩니다.
- 세션 안에서만 쓸 거라면 `CREATE READABLE EXTERNAL TEMP TABLE` 로 만들어 세션이
  끝날 때 사라지게 하는 편이 뒷정리가 쉽습니다.

## 4. 파일 개수와 병렬성

**가장 중요한 튜닝 포인트입니다.** Greenplum은 S3 오브젝트를 세그먼트에 나눠
할당하므로, 파일이 하나면 세그먼트 하나만 일합니다.

`query-to-csv` 는 결과를 파일 하나로 씁니다. 세그먼트를 다 쓰려면 쿼리를 파티션
조건으로 쪼개 여러 번 실행해 파일을 나누세요.

```bash
for dt in 2026-08-01 2026-08-02 2026-08-03; do
    bin/query-to-csv \
        --query "SELECT * FROM sales.orders WHERE dt = '$dt'" \
        --output "out/orders-$dt.csv.gz" --gzip \
        --delimiter $'\t' --null-string '\N' --no-header
done
bin/s3-ops upload ./out/ s3://dw-stage/orders/202608/ --recursive
```

총 데이터가 10GB일 때 파일이 80개면 세그먼트가 16대여도 충분히 고르게 퍼집니다.
반대로 파일이 3개뿐이면 나머지 13대는 놀게 됩니다.

세그먼트 수는 다음으로 확인합니다.

```sql
SELECT count(*) FROM gp_segment_configuration WHERE content >= 0 AND role = 'p';
```

올라간 파일 개수와 크기 분포는 `bin/s3-ops ls` 로 확인할 수 있습니다.

## 5. 정리

적재가 끝나면 S3 파일을 지웁니다. 접두사를 실행 단위로 나눠 뒀다면 그 디렉터리만
통째로 지우면 됩니다.

```bash
bin/s3-ops rmdir s3://dw-stage/orders/2026-08-01/          # 지울 목록을 보여주고 물어봅니다
bin/s3-ops rmdir s3://dw-stage/orders/2026-08-01/ --yes    # 확인 없이
```

`rmdir` 은 그 접두사로 시작하는 오브젝트를 **전부** 지웁니다. 접두사가 비어 있으면
(`s3://버킷/`) 버킷 전체 삭제를 막기 위해 거부합니다.

디버깅할 때는 지우지 말고 파일을 직접 열어보세요. 목록과 내용을 확인하는 방법은
[boto3로 S3 버킷·파일 목록 보기](boto3.md)에 정리해 두었습니다.

Greenplum 쪽 `INSERT ... SELECT` 는 한 트랜잭션이라 실패하면 롤백됩니다. S3 파일만
정리하면 깨끗한 상태로 되돌아갑니다.

## 6. 형식 오류 허용

원본에 깨진 값이 섞여 있어 일부 행을 버리고 진행하고 싶다면 외부 테이블에
`LOG ERRORS` 를 붙입니다.

```sql
CREATE READABLE EXTERNAL TABLE ext_orders_20260801 (...)
LOCATION (...)
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N' ESCAPE E'\\')
LOG ERRORS SEGMENT REJECT LIMIT 100 ROWS;
```

100건까지는 오류 행을 건너뜁니다. 버려진 행은 다음으로 확인합니다.

```sql
SELECT * FROM gp_read_error_log('ext_orders_20260801');
```

`LOG ERRORS` 를 붙이지 않으면 오류가 하나라도 나면 즉시 실패합니다. 데이터 정합성이
중요하면 붙이지 마세요.

## PXF를 쓰는 경우

내장 `s3` 프로토콜 대신 PXF가 이미 구축되어 있다면 LOCATION만 바뀝니다.

```sql
LOCATION ('pxf://dw-stage/orders/2026-08-01/?PROFILE=s3:text&SERVER=s3srv')
```

`endpoint` 나 `config=` 는 필요 없고, 대신 PXF 서버 쪽 `s3-site.xml` 에 자격증명이
설정되어 있어야 합니다.

프로파일 정의(`pxf-profiles.xml`), 서버 설정(`s3-site.xml`), 외부 테이블 LOCATION이
각각 어떤 역할을 하는지는 [PXF로 S3 읽기 설정](pxf.md)에 정리해 두었습니다.
