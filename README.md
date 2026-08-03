# impala-to-whpg

Impala에서 SELECT한 결과를 Greenplum에 적재하는 Python ETL 도구입니다.

행 단위 `INSERT`는 물론이고 마스터를 통과하는 `COPY`조차 병목이 되는 규모를
전제로, 기본 적재 경로는 **S3 스테이징 + 외부 테이블**입니다. Impala 결과를 S3에
gzip 파일로 나눠 올린 뒤 Greenplum 세그먼트가 자기 몫을 병렬로 직접 읽습니다.

```
INSERT : 행마다 왕복                      — 느림
COPY   : Impala → 파이썬 → 마스터 → 세그먼트  — 마스터가 병목
S3     : Impala → 파이썬 → S3 → 세그먼트 N대가 병렬로 직접 읽기
```

두 방식 모두 지원하며 작업별로 `load_method`로 고릅니다. 어느 쪽이든 Impala 커서에서
읽은 행을 메모리에 쌓지 않고 스트리밍하므로, 수억 건을 적재해도 메모리 사용량은
파일 하나 수준으로 유지됩니다.

## 설치

```bash
pip install -r requirements.txt
```

- `pure-sasl`, `thrift-sasl`은 **LDAP(PLAIN)과 Kerberos(GSSAPI) 인증 모두에 필요합니다.**
  impyla는 `auth_mechanism`이 `NOSASL`이 아니면 접속하는 순간 이 둘을 불러옵니다.
- S3 방식을 쓰려면 Greenplum 세그먼트에 `s3` 프로토콜 설정 파일을 배포해야 합니다.
  준비 절차는 [S3 외부 테이블 적재 설정](docs/s3_external_table.md)에 정리해 두었습니다.

### 설치 중 자주 막히는 것

| 증상 | 해결 |
| --- | --- |
| `Failed building wheel for pure-sasl` | 데비안/우분투 setuptools 문제입니다. `pip install --use-pep517 pure-sasl thrift-sasl` |
| `ModuleNotFoundError: No module named 'impala'` | impyla 미설치. `pip install impyla` |
| `ModuleNotFoundError: ...; 'impala' is not a package` | 작업 디렉터리에 `impala.py` 파일이 있어 impyla를 가리고 있습니다. 파일 이름을 바꾸세요. |
| 접속 시 `No module named 'thrift_sasl'` | LDAP/Kerberos에 필요한 SASL 패키지가 없습니다. 위 첫 줄대로 설치하세요. |

앞의 세 가지는 실행 시 원인과 해결 방법을 함께 출력하므로, 메시지를 그대로 따라가면
됩니다.

## 빠른 시작

`config.yaml`을 복사해 접속 정보를 채운 뒤 실행합니다.

```bash
cp config.yaml config.local.yaml

export IMPALA_USER='etl_user'     # Impala LDAP 계정
export IMPALA_PASSWORD='...'
export GP_PASSWORD='...'

python -m impala_to_greenplum --config config.local.yaml            # 전체 작업 실행
python -m impala_to_greenplum --config config.local.yaml -j orders  # 특정 작업만 실행
python -m impala_to_greenplum --config config.local.yaml -v         # 디버그 로그
```

예제 설정은 Impala에 **TLS + LDAP**으로 접속합니다(`auth_mechanism: PLAIN`,
`use_ssl: true`). 인증이 없는 환경이라면 `auth_mechanism: NOSASL`로 바꾸고
`user`/`password`/`use_ssl`/`ca_cert`를 지우세요. Kerberos는 `GSSAPI`입니다.

### 설정 파일 두 개의 역할

| 파일 | 용도 |
| --- | --- |
| `config.yaml` | 바로 돌려볼 수 있는 최소 예제. 저장소에 커밋됩니다. |
| `config.example.yaml` | 쓸 수 있는 모든 옵션을 주석과 함께 나열한 참조 문서. |
| `config.local.yaml` | 실제 운영 값. `.gitignore`에 걸려 있어 커밋되지 않습니다. |

**운영 값은 반드시 `config.local.yaml`에 두세요.** `config.yaml`은 커밋되는 파일이라
여기에 실제 호스트나 비밀번호를 적으면 저장소에 그대로 올라갑니다.

비밀번호 같은 민감한 값은 어느 파일에서든 YAML에 직접 쓰지 말고 `${GP_PASSWORD}` 또는
`${GP_USER:-etl}` 형태로 환경변수를 참조하세요. 정의되지 않은 환경변수를 참조하면
실행 시점에 바로 오류가 나므로, 값이 비어 있는 채로 접속을 시도하는 일은 없습니다.

## 코드에서 직접 호출하기

```python
from impala_to_greenplum import GreenplumConfig, ImpalaConfig, S3Config
from impala_to_greenplum.pipeline import load_query

result = load_query(
    ImpalaConfig(host="impala.example.com", database="sales"),
    GreenplumConfig(host="gp.example.com", database="dw", user="etl",
                    password="...", schema="staging"),
    query="SELECT order_id, customer_id, amount FROM sales.orders WHERE dt = '2026-08-01'",
    target_table="orders",
    s3=S3Config(                       # 생략하면 COPY 방식으로 동작한다
        bucket="dw-stage",
        endpoint="s3.ap-northeast-2.amazonaws.com",
        region="ap-northeast-2",
        gp_config="/home/gpadmin/s3.conf",
    ),
    mode="upsert",
    key_columns=["order_id"],
)
print(result.summary())
# orders: 1,240,331건 읽음 / 1,240,331건 적재 / 18,204건 삭제, 42.7초 (29,047 rows/s)
```

전체 예제는 `examples/simple_load.py`를 참고하세요.

## 적재 모드

| 모드 | 동작 |
| --- | --- |
| `append` | 기존 데이터를 유지하고 뒤에 붙입니다. |
| `truncate` | `TRUNCATE` 후 적재합니다. 스키마와 권한은 그대로 유지됩니다. |
| `replace` | 테이블을 `DROP` 후 Impala 스키마를 기준으로 다시 만들고 적재합니다. |
| `upsert` | 임시 스테이징 테이블에 적재한 뒤 `key_columns` 기준으로 기존 행을 지우고 삽입합니다. |

`upsert`는 Greenplum 6가 `INSERT ... ON CONFLICT`를 지원하지 않기 때문에
`DELETE ... USING` + `INSERT ... SELECT` 조합으로 처리합니다. 두 구문 모두 같은
트랜잭션 안에서 실행되므로 원자적으로 반영됩니다.

## 주요 작업 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `query` / `source_table` | – | 둘 중 하나만 지정합니다. `source_table`은 `SELECT *`로 읽습니다. |
| `target_table` | – | 적재 대상 Greenplum 테이블(필수). |
| `mode` | `append` | 위 표 참고. |
| `key_columns` | `[]` | `upsert`의 병합 키(필수). |
| `batch_size` | `50000` | Impala `fetchmany` 크기. |
| `create_target_if_missing` | `true` | 대상 테이블이 없으면 Impala 스키마로 생성합니다. |
| `distributed_by` | `[]` | 테이블 생성 시 분산키. 미지정 시 `key_columns` → 첫 컬럼 순으로 추론합니다. ([선정 가이드](docs/distribution_key.md)) |
| `target_columns` | `[]` | SELECT 결과와 대상 컬럼명이 다를 때 순서대로 매핑합니다. |
| `analyze_after_load` | `true` | 적재 후 `ANALYZE` 실행. |
| `load_method` | `s3` | `s3`(S3 업로드 후 외부 테이블) 또는 `copy`(`COPY FROM STDIN`). |

S3 관련 설정은 작업이 아니라 최상위 `s3` 섹션에 둡니다. 전체 옵션은
[S3 외부 테이블 적재 설정](docs/s3_external_table.md)을 참고하세요.

## 동작 방식

1. `SELECT ... LIMIT 0`으로 결과 스키마만 먼저 읽어 컬럼과 타입을 파악합니다.
2. 모드에 따라 대상 테이블을 준비합니다(생성 / `TRUNCATE` / 재생성).
3. Impala 커서를 `fetchmany`로 배치 조회하면서 행을 COPY TEXT 포맷으로 인코딩합니다.
   여기까지는 두 방식이 같고, 이후가 갈립니다.
   - **s3**: 인코딩한 행을 gzip 파일 여러 개로 나눠 S3에 올립니다. 한 파일을 채우는
     동안 앞서 채운 파일이 백그라운드로 업로드되어 조회 시간에 네트워크 대기가
     묻힙니다. 업로드가 끝나면 그 접두사를 가리키는 외부 테이블을 만들고
     `INSERT ... SELECT`로 옮깁니다. 이때 각 세그먼트가 자기 몫의 파일을 직접 읽습니다.
   - **copy**: 인코딩한 행을 파일 객체처럼 `copy_expert`에 넘깁니다. psycopg2가
     `read()`를 호출할 때마다 필요한 만큼만 Impala에서 꺼내오는 지연 스트리밍입니다.
4. DDL 준비부터 적재, 병합까지 한 트랜잭션에서 처리하고, 실패하면 전부 롤백합니다.
   S3 파일은 성공·실패와 무관하게 정리합니다(`cleanup: false`로 남길 수 있습니다).

CSV 대신 COPY의 기본 TEXT 포맷을 쓰는 이유는 빈 문자열과 `NULL`이 명확히 구분되기
때문입니다(`NULL`은 `\N`). 탭·개행·백슬래시는 인코딩 단계에서 이스케이프하므로
값 안에 구분자가 들어 있어도 필드 경계가 깨지지 않습니다. 외부 테이블도 같은 포맷을
쓰기 때문에 두 경로가 동일한 데이터를 만듭니다.

### 어느 쪽을 쓸까

| | `s3` | `copy` |
| --- | --- | --- |
| 적합한 규모 | 수백만 건 이상 | 수만~수십만 건 |
| 병렬성 | 세그먼트 수만큼 | 마스터 1대 |
| 사전 준비 | 세그먼트에 `s3.conf` 배포 필요 | 없음 |
| 추가 비용 | S3 왕복, 스토리지 | 없음 |

소량 데이터는 S3 왕복 오버헤드 때문에 `copy`가 오히려 빠릅니다.

## 타입 매핑

| Impala | Greenplum |
| --- | --- |
| `string`, `array<...>`, `map<...>`, `struct<...>` | `text` |
| `tinyint`, `smallint` | `smallint` |
| `int` | `integer` |
| `bigint` | `bigint` |
| `float` | `real` |
| `double` | `double precision` |
| `decimal(p,s)` | `numeric(p,s)` |
| `char(n)`, `varchar(n)` | `varchar(n)` |
| `boolean` / `timestamp` / `date` / `binary` | `boolean` / `timestamp` / `date` / `bytea` |

매핑되지 않은 타입은 정보 손실을 피하기 위해 `text`로 떨어집니다. 자동 생성한 DDL이
마음에 들지 않으면 대상 테이블을 직접 만들고 `create_target_if_missing: false`로 두세요.

## 성능 팁

- `batch_size`는 5만~10만 건 사이가 무난합니다. 행이 넓으면 줄이세요.
- 대량 적재 시 `session_sql`에 `SET gp_autostats_mode = 'none'`을 넣고
  `analyze_after_load`로 한 번만 통계를 갱신하는 편이 빠릅니다.
- Impala 쪽 메모리가 빠듯하면 `session_settings.MEM_LIMIT`을 조정하세요.
- 큰 테이블은 파티션 조건(`WHERE dt = ...`)으로 작업을 나눠 병렬 실행하는 것이
  단일 스트림보다 빠릅니다.
- 분산키가 한쪽으로 쏠리면 적재와 이후 조회가 모두 느려집니다. 후보 컬럼을 진단하는
  쿼리는 [분산키 선정 가이드](docs/distribution_key.md)에 정리해 두었습니다.
- **S3 방식에서 가장 중요한 건 파일 개수입니다.** 파일이 세그먼트 수보다 적으면
  놀고 있는 세그먼트가 생깁니다. `s3.file_size_mb`를 줄여 더 잘게 나누세요.
  파이프라인이 부족하면 경고를 남깁니다.

## 문서

- [S3 외부 테이블 적재 설정](docs/s3_external_table.md) — `s3.conf` 배포, 파일 분할, 오류 허용
- [분산키 선정 가이드](docs/distribution_key.md) — 후보 컬럼 진단 쿼리
- [boto3로 S3 버킷·파일 목록 보기](docs/boto3.md) — 버킷 확인, 스테이징 파일 조회, 찌꺼기 정리

## 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

가짜 DB-API 연결과 가짜 S3 클라이언트로 인코딩, 트랜잭션 경계, 파일 분할, 정리 동작,
각 적재 모드가 만들어내는 SQL을 검증하므로 실제 Impala/Greenplum/S3 없이도 실행됩니다.

## 프로젝트 구조

```
impala_to_greenplum/
  config.py       # YAML + 환경변수 기반 설정 로딩과 검증
  source.py       # Impala 접속 및 배치 조회
  copy_stream.py  # 행 → COPY TEXT 인코딩, 바이트 스트림 어댑터
  s3_stage.py     # S3 분할 업로드, 외부 테이블 LOCATION 생성, 정리
  target.py       # Greenplum 외부 테이블/COPY 적재, 스테이징, 병합, DDL
  typemap.py      # Impala → Greenplum 타입 매핑과 식별자 인용
  pipeline.py     # 작업 실행 흐름과 트랜잭션 제어
  cli.py          # 커맨드라인 진입점

examples/
  simple_load.py        # 설정 파일 없이 코드로 적재
  query_to_csv.py       # Impala 쿼리 → CSV 저장 (TLS + LDAP, 구간별 시간 측정)
  s3_ops.py             # S3 업로드·삭제·디렉터리 생성/삭제·목록
  list_staged_files.py  # S3 스테이징 파일 목록 확인
```

## S3 파일 다루기

`examples/s3_ops.py`로 업로드, 삭제, 디렉터리 생성/삭제를 할 수 있습니다.

```bash
python examples/s3_ops.py ls     s3://dw-stage/impala/
python examples/s3_ops.py upload orders.csv s3://dw-stage/impala/
python examples/s3_ops.py upload ./out/ s3://dw-stage/impala/out/ --recursive
python examples/s3_ops.py mkdir  s3://dw-stage/impala/2026-08-03/
python examples/s3_ops.py rm     s3://dw-stage/impala/orders.csv --yes
python examples/s3_ops.py rmdir  s3://dw-stage/impala/out/ --yes
```

**S3에는 디렉터리가 없습니다.** 키가 `a/b/c.csv`인 오브젝트가 있을 뿐이고, 콘솔이
슬래시를 보고 폴더처럼 보여줄 뿐입니다. 그래서 이 스크립트에서는

- `mkdir`은 `a/b/`라는 **빈 오브젝트**를 만듭니다. 콘솔에서 빈 폴더로 보이게 하는
  용도이고, 파일을 올릴 때 상위 디렉터리를 미리 만들 필요는 없습니다.
- `rmdir`은 그 접두사로 시작하는 오브젝트를 **전부** 지웁니다.

삭제는 되돌릴 수 없어서 안전장치를 뒀습니다.

- `--yes` 없이 실행하면 지울 목록을 보여주고 물어봅니다. 터미널이 아니면 거부합니다.
- `-n`/`--dry-run`으로 무엇을 지울지만 확인할 수 있습니다.
- `rmdir`에 접두사가 비어 있으면(`s3://버킷/`) **버킷 전체 삭제를 막기 위해 거부**합니다.

접속 정보는 환경변수나 IAM 역할을 따르고, `--config config.yaml`을 주면 프로젝트
설정의 `s3` 섹션을 그대로 재사용합니다. MinIO 등은 `--endpoint-url`을 쓰세요.

## Impala 쿼리를 CSV로 내려받기

Greenplum 적재와 별개로, Impala 결과를 파일로 뽑아야 할 때가 있습니다.
`examples/query_to_csv.py`가 TLS + LDAP 접속으로 조회해 CSV로 저장하고, 어느 구간에
시간을 썼는지 보여줍니다.

**이 스크립트는 단독으로 동작합니다.** 표준 라이브러리와 impyla 외에 아무것도
필요하지 않으므로, 이 파일 하나만 복사해서 다른 곳에서 써도 됩니다.

```bash
pip install impyla pure-sasl thrift-sasl
export IMPALA_PASSWORD='...'

python examples/query_to_csv.py \
    --host impala.example.com --user etl_user \
    --ca-cert /etc/ssl/certs/impala-ca.pem \
    --query "SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'" \
    --output orders.csv
```

```
=== 구간별 소요 시간 ===
  1. Impala 접속        0.412초    2.1%
  2. 쿼리 실행 요청     1.203초    6.1%
  3. 첫 배치 대기       8.442초   42.6%
  4. 데이터 수신        6.120초   30.9%
  5. CSV 쓰기           3.640초   18.4%
     기타              0.002초    0.0%
  ───────────────────────────────────
     합계             19.817초  100.0%

orders.csv  182.4MB  1,240,331행
평균 62,600 rows/s
```

병목이 Impala 쪽인지(첫 배치 대기), 네트워크인지(데이터 수신), 로컬 디스크인지
(CSV 쓰기) 한눈에 구분됩니다.

- 비밀번호는 `--password` 같은 인자로 받지 않습니다. `ps`로 다른 사용자에게 노출되기
  때문에 환경변수(`IMPALA_PASSWORD`)나 대화형 입력으로만 받습니다.
- `--ca-cert`를 주지 않으면 통신은 암호화되지만 서버 인증서를 검증하지 않습니다.
  운영 환경에서는 CA 인증서 경로를 지정하세요.
- 엑셀에서 한글이 깨지면 `--encoding utf-8-sig`를 쓰세요.
- 그 밖에 `--gzip`, `--null-string`, `--query-file`, `--set KEY=VALUE`를 지원합니다.
  자세한 건 `--help`를 보세요.

### 구분자와 따옴표

기본 구분자는 **백틱(`` ` ``)** 이고, 값을 따옴표로 **감싸지 않습니다.**

```
order_id`name`amount`order_dt
1`김철수`10.50`2026-08-01
2`쉼표, 및 따옴표" 포함``2026-08-02
```

쉼표나 큰따옴표가 값에 들어 있어도 그대로 나갑니다. 구분자를 백틱으로 두면 일반적인
텍스트에 거의 나타나지 않아 따옴표 없이도 안전합니다.

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--delimiter` | `` ` `` | 컬럼 구분자. `--delimiter ,` 나 `--delimiter $'\t'` 도 됩니다. |
| `--quote` | 꺼짐 | 켜면 필요할 때 값을 `"`로 감쌉니다(`QUOTE_MINIMAL`). |
| `--escapechar` | `\` | 따옴표를 쓰지 않을 때 값 안의 구분자를 이스케이프할 문자. |

```bash
# 따옴표로 감싸기
python examples/query_to_csv.py ... --quote

# 쉼표 구분으로 되돌리기
python examples/query_to_csv.py ... --delimiter ,
```

값 안에 구분자가 들어 있으면 이스케이프됩니다(`백틱` → `` 백틱\`포함 ``). `--escapechar ''`
로 끌 수 있지만, 그 상태에서 값에 구분자가 나오면 **오류로 중단**됩니다. 이스케이프
문자가 들어간 값은 한 번 더 이스케이프되므로(`\N` → `\\N`), `--null-string '\N'`처럼
쓸 때는 `--escapechar ''`를 함께 고려하세요.

따옴표를 쓰지 않으면 값 안의 **줄바꿈이 레코드를 깨뜨립니다.** 줄바꿈이 들어갈 수 있는
컬럼이라면 `--quote`를 켜거나 쿼리에서 `regexp_replace`로 걷어내세요.

### SQL 파일 지정하기

`--query-file`로 여러 줄짜리 쿼리를 담은 `.sql` 파일을 넘길 수 있습니다.

```bash
python examples/query_to_csv.py --host ... --user ... \
    --query-file daily_orders.sql --output orders.csv
```

**파일 내용은 그대로 실행합니다.** 문장을 쪼개거나 세미콜론을 떼어내지 않으므로,
여러 줄로 이어진 쿼리도 주석도 작성한 그대로 서버에 전달됩니다.

파일을 읽을 때 두 가지만 인코딩 차원에서 처리합니다. SQL을 고치는 게 아니라 파일을
제대로 해석하는 것입니다.

- **BOM** — `utf-8-sig`로 읽어 윈도우 편집기가 붙이는 `U+FEFF`를 벗깁니다. 이게 남으면
  쿼리 첫 글자 앞에 보이지 않는 문자가 끼어 syntax error가 납니다. `strip()`으로는
  지워지지 않으니 주의하세요.
- **CRLF** — 윈도우 줄바꿈을 `\n`으로 읽습니다(파이썬 텍스트 모드 기본 동작).

`--debug`를 주면 **실제로 서버에 보내는 SQL**을 출력하므로, 파일이 의도대로 읽혔는지
바로 확인할 수 있습니다. 문법 오류가 나면 끝의 세미콜론이나 여러 문장 여부를 짚어
줍니다.

```
--- 실행할 SQL ---
SELECT
    order_id,
    amount
FROM sales.orders
WHERE dt = '2026-08-01'
------------------
```

### Thrift EOF 오류가 날 때

`TSocket read 0 bytes`, `end of file` 같은 오류는 **서버가 핸드셰이크 도중 연결을
끊었다**는 뜻입니다. 인증 실패가 아니라 포트·전송 방식·TLS·인증 방식 중 하나가
서버 설정과 어긋난 경우가 대부분입니다. 스크립트가 현재 설정과 함께 점검 목록을
출력하니 위에서부터 하나씩 확인하세요.

| 포트 | 용도 | 접속 방법 |
| --- | --- | --- |
| 21050 | 바이너리 HS2 | 기본값 |
| 28000 | HTTP HS2 | `--http-transport --port 28000` |
| 21000 | 예전 beeswax | 여기 붙으면 EOF |
| 25000 | 웹 UI | 여기 붙으면 EOF |

```bash
# HTTP 엔드포인트를 쓰는 환경 (CDP 등에서 흔합니다)
python examples/query_to_csv.py --host impala.example.com --user etl_user \
    --port 28000 --http-transport --ca-cert /etc/ssl/certs/impala-ca.pem \
    -q "SELECT 1" -o test.csv

# 서버가 평문이라면
python examples/query_to_csv.py ... --no-ssl

# 인증이 없는 서버라면
python examples/query_to_csv.py ... --auth-mechanism NOSASL
```

`impala-shell`로 같은 조건에서 붙어보면 서버 쪽 설정인지 클라이언트 쪽인지 빨리
구분됩니다. `--debug`를 주면 SASL 핸드셰이크가 어디까지 진행됐는지 로그로 볼 수
있습니다.

### SASL에 대해

바이너리 전송에서 `--auth-mechanism PLAIN`(기본)은 **SASL PLAIN**으로 동작합니다.
`NOSASL`만 SASL을 쓰지 않습니다. HTTP 전송은 SASL 대신 HTTP 기본 인증 헤더를 씁니다.

SASL 구현은 impyla가 `puresasl`을 기본으로 씁니다. Cyrus SASL(`sasl` 패키지)을
쓰려면 `--sasl-backend sasl`을 주면 되지만, **`sasl` 패키지는 Python 3.11 이상에서
빌드되지 않습니다**(saslwrapper가 3.11에서 없어진 `longintrepr.h`를 참조합니다).
Python 3.11 이상에서는 기본값인 `puresasl`을 쓰세요.
