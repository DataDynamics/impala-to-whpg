# impala-to-whpg

Impala에서 SELECT한 결과를 Greenplum에 적재하는 Python ETL 도구입니다.

Impala 커서에서 읽은 행을 메모리에 쌓지 않고 곧바로 Greenplum의
`COPY ... FROM STDIN` 스트림으로 흘려보냅니다. 수억 건을 적재해도 메모리 사용량은
배치 하나 수준으로 유지되고, `INSERT` 반복 방식보다 훨씬 빠릅니다.

## 설치

```bash
pip install -r requirements.txt
```

Kerberos 환경이라면 `requirements.txt`의 `pure-sasl`, `thrift-sasl` 주석을 해제하세요.

## 빠른 시작

`config.example.yaml`을 복사해 접속 정보를 채운 뒤 실행합니다.

```bash
cp config.example.yaml config.yaml
export GP_PASSWORD='...'

python -m impala_to_greenplum --config config.yaml            # 전체 작업 실행
python -m impala_to_greenplum --config config.yaml -j orders  # 특정 작업만 실행
python -m impala_to_greenplum --config config.yaml -v         # 디버그 로그
```

비밀번호 같은 민감한 값은 YAML에 직접 쓰지 말고 `${GP_PASSWORD}` 또는
`${GP_USER:-etl}` 형태로 환경변수를 참조하세요.

## 코드에서 직접 호출하기

```python
from impala_to_greenplum import GreenplumConfig, ImpalaConfig
from impala_to_greenplum.pipeline import load_query

result = load_query(
    ImpalaConfig(host="impala.example.com", database="sales"),
    GreenplumConfig(host="gp.example.com", database="dw", user="etl",
                    password="...", schema="staging"),
    query="SELECT order_id, customer_id, amount FROM sales.orders WHERE dt = '2026-08-01'",
    target_table="orders",
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
| `distributed_by` | `[]` | 테이블 생성 시 분산키. 미지정 시 `key_columns` → 첫 컬럼 순으로 추론합니다. |
| `target_columns` | `[]` | SELECT 결과와 대상 컬럼명이 다를 때 순서대로 매핑합니다. |
| `analyze_after_load` | `true` | 적재 후 `ANALYZE` 실행. |

## 동작 방식

1. `SELECT ... LIMIT 0`으로 결과 스키마만 먼저 읽어 컬럼과 타입을 파악합니다.
2. 모드에 따라 대상 테이블을 준비합니다(생성 / `TRUNCATE` / 재생성).
3. Impala 커서를 `fetchmany`로 배치 조회하면서, 각 행을 COPY TEXT 포맷으로 인코딩해
   `copy_expert`에 파일 객체처럼 넘깁니다. psycopg2가 `read()`를 호출할 때마다
   필요한 만큼만 Impala에서 꺼내오는 지연 스트리밍 방식입니다.
4. DDL 준비부터 COPY, 병합까지 한 트랜잭션에서 처리하고, 실패하면 전부 롤백합니다.

CSV 대신 COPY의 기본 TEXT 포맷을 쓰는 이유는 빈 문자열과 `NULL`이 명확히 구분되기
때문입니다(`NULL`은 `\N`). 탭·개행·백슬래시는 인코딩 단계에서 이스케이프하므로
값 안에 구분자가 들어 있어도 필드 경계가 깨지지 않습니다.

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

## 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

가짜 DB-API 연결로 COPY 인코딩, 트랜잭션 경계, 각 적재 모드가 만들어내는 SQL을
검증하므로 실제 Impala/Greenplum 없이도 실행됩니다.

## 프로젝트 구조

```
impala_to_greenplum/
  config.py       # YAML + 환경변수 기반 설정 로딩과 검증
  source.py       # Impala 접속 및 배치 조회
  copy_stream.py  # 행 이터레이터 → COPY TEXT 바이트 스트림 어댑터
  target.py       # Greenplum COPY 적재, 스테이징, 병합, DDL
  typemap.py      # Impala → Greenplum 타입 매핑과 식별자 인용
  pipeline.py     # 작업 실행 흐름과 트랜잭션 제어
  cli.py          # 커맨드라인 진입점
```
