# impala-to-whpg

Impala 조회, Greenplum 실행, S3 조작을 위한 명령행 도구 모음입니다.

| 도구 | 하는 일 |
| --- | --- |
| `bin/impala-query` | Impala에 쿼리를 실행하고 결과를 표나 CSV로 보여줍니다. 구간별 소요 시간도 함께. |
| `bin/gp-query` | Greenplum에 SQL을 실행하고 결과를 표나 CSV로 보여줍니다. |
| `bin/s3-ops` | S3 업로드·삭제·디렉터리 생성/삭제·목록. |

pip로 설치하지 않고 저장소에서 바로 실행합니다. `bin/` 아래 스크립트가 소스 위치를
찾아 파이썬에 넘겨주므로 어느 디렉터리에서 호출해도 동작합니다. 다른 인터프리터를
쓰려면 `PYTHON=/path/to/python bin/s3-ops ...` 처럼 지정하세요.

**인자 없이 실행하면 사용법과 예시를 보여줍니다.** `--help`와 같습니다.

```bash
bin/impala-query
bin/gp-query
bin/s3-ops
```

## 설치

```bash
pip install -r requirements.txt
```

도구마다 필요한 것이 다릅니다. 쓰지 않는 쪽은 설치하지 않아도 됩니다.

| 도구 | 필요한 패키지 |
| --- | --- |
| `impala-query` | `impyla`, `PyYAML`, `Jinja2` (+ LDAP/Kerberos 인증 시 `pure-sasl`, `thrift-sasl`) |
| `gp-query` | `psycopg2-binary`, `PyYAML`, `Jinja2` |
| `s3-ops` | `boto3`, `PyYAML` |

`pure-sasl`, `thrift-sasl`은 **LDAP(PLAIN)과 Kerberos(GSSAPI) 인증 모두에 필요합니다.**
impyla는 `auth_mechanism`이 `NOSASL`이 아니면 접속하는 순간 이 둘을 불러옵니다.

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

**`bin/` 아래 스크립트는 아무것도 지정하지 않아도 `conf/config.yaml`을 읽습니다.**
접속 정보를 한 번 채워두면 명령마다 반복해서 줄 필요가 없습니다.

```bash
# 설정 파일의 ${...} 가 참조하는 환경변수를 먼저 채웁니다
export IMPALA_USER='etl_user'
export IMPALA_PASSWORD='...'
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'

# 접속 정보는 설정에서, 쿼리는 sql/ 에서 오므로 변수와 출력 경로만 주면 됩니다
bin/impala-query -f daily_orders.sql --var dt=2026-08-01 --output orders.csv

# Greenplum에 실행. 결과가 있으면 표로 보여줍니다.
bin/gp-query -f order_summary.sql --var dt=2026-08-01

# 버킷도 설정에서 옵니다
bin/s3-ops ls orders/
```

경로는 스크립트 위치를 기준으로 잡으므로 **어느 디렉터리에서 실행해도 같은 파일**을
읽습니다.

```bash
bin/s3-ops --config conf/config.local.yaml ls orders/   # 다른 파일을 쓸 때
bin/s3-ops --no-config --bucket dw-stage ls orders/     # 설정을 무시할 때
```

### 우선순위

**명령행 인자 > 설정 파일 > 각 스크립트의 기본값** 입니다. 설정에 값이 있어도
명령행으로 준 값이 항상 이깁니다.

```bash
bin/impala-query --host other-impala.example.com -q "SELECT 1" -o out.csv
# host 만 바뀌고 user, ca_cert, session_settings 등은 설정 값을 그대로 씁니다
```

설정 파일에도 명령행에도 없는 값은 기본값으로 떨어집니다. 접속에 반드시 필요한
값(Impala의 `host`/`user`, Greenplum의 `host`/`database`/`user`, S3의 버킷)이 끝까지
비어 있을 때만 오류가 납니다.

### 설정 파일 세 개의 역할

| 파일 | 용도 |
| --- | --- |
| `conf/config.yaml` | **기본으로 읽는 파일.** 저장소에 커밋됩니다. |
| `conf/config.example.yaml` | 쓸 수 있는 모든 옵션을 주석과 함께 나열한 참조 문서. |
| `conf/config.local.yaml` | `--config`로 지정해서 쓰는 로컬 값. `.gitignore`에 걸려 있어 커밋되지 않습니다. |

`conf/config.yaml`은 커밋되는 파일입니다. **비밀번호나 액세스 키를 여기에 직접 적으면
저장소에 그대로 올라갑니다.** `${IMPALA_PASSWORD}`, `${AWS_SECRET_ACCESS_KEY}` 처럼
환경변수를 참조하세요. `${AWS_DEFAULT_REGION:-ap-northeast-2}` 형태로 기본값도 줄 수
있습니다.

호스트명처럼 커밋하기 곤란한 값이 있다면 `conf/config.local.yaml`에 두고
`--config conf/config.local.yaml`로 지정하세요. 이 파일은 자동으로 읽히지 않습니다.

참조한 환경변수가 정의되어 있지 않으면 **그 항목만 건너뛰고 경고를 남깁니다.**
설정 파일을 항상 읽기 때문에, 지금 쓰는 명령과 무관한 항목의 환경변수 하나가 비어
있다고 실행 자체가 막히면 곤란하기 때문입니다.

```
경고: 환경변수 IMPALA_PASSWORD 가 정의되지 않아 설정값을 건너뜁니다.
```

## 소요 시간과 진행 상황

**세 스크립트 모두 작업이 끝나면 구간별 소요 시간을 출력합니다.** 성공하든 실패하든
항상 나오므로, 실패했을 때 어디까지 갔는지도 알 수 있습니다.

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
```

오래 걸리는 구간에서는 진행 상황도 보여줍니다. 조회 행 수, 업로드·삭제 개수입니다.

```
  받는 중 1,300,000행  412,331행/s  3.2초
```

**보고는 전부 stderr로 나갑니다.** stdout은 조회 결과 같은 데이터 몫이라, 파이프로
넘기거나 파일로 받을 때 섞이지 않습니다.

```bash
bin/gp-query -q "SELECT * FROM t" > data.txt     # data.txt 에는 표만 들어갑니다
bin/gp-query -q "SELECT * FROM t" 2> /dev/null   # 보고만 버립니다
```

`--no-progress`로 진행 상황만 끌 수 있습니다. 소요 시간 요약은 그대로 나옵니다.

## 크론에서 돌리기

**crontab 항목은 한 줄이어야 합니다.** 셸과 달리 백슬래시로 줄을 이을 수 없고,
`%`는 개행으로 해석되므로 `\%`로 이스케이프해야 합니다.

```cron
0 3 * * * /srv/impala-to-whpg/bin/impala-query -f daily_orders.sql -V dt=$(date -d yesterday +\%Y-\%m-\%d) -o /data/orders.csv >> /var/log/etl.log 2>&1
```

길어지면 셸 스크립트로 감싸는 편이 읽기 쉽습니다.

```bash
#!/bin/bash
# /srv/etl/daily-orders.sh
set -euo pipefail
export IMPALA_USER=etl_user IMPALA_PASSWORD="$(cat /etc/etl/impala.pw)"
dt="$(date -d yesterday +%Y-%m-%d)"

/srv/impala-to-whpg/bin/impala-query -f daily_orders.sql -V "dt=$dt" \
    -o "/data/orders-$dt.csv.gz" --gzip --delimiter $'\t' --null-string '\N' --no-header
/srv/impala-to-whpg/bin/s3-ops upload "/data/orders-$dt.csv.gz" "s3://dw-stage/orders/$dt/"
```

```cron
0 3 * * * /srv/etl/daily-orders.sh >> /var/log/etl.log 2>&1
```

크론에서 문제가 되는 것들은 스크립트 쪽에서 처리해 두었습니다.

| 크론에서 생기는 문제 | 처리 |
| --- | --- |
| 작업 디렉터리가 임의 | 설정·SQL·인증서 경로를 **스크립트 위치 기준**으로 잡습니다. 어디서 실행해도 같은 파일을 씁니다. |
| `LANG`이 없어 인코딩이 `ascii` | 래퍼가 `PYTHONIOENCODING=utf-8`을 넣습니다. 없으면 한글 출력에서 `UnicodeEncodeError`로 죽습니다. |
| 출력이 버퍼에 갇힘 | 래퍼가 `PYTHONUNBUFFERED=1`을 넣습니다. 중간에 죽어도 로그가 남습니다. |
| 진행 상황이 로그를 뒤덮음 | 터미널이 아니면 `\r` 대신 줄을 쓰고, 갱신 간격을 30초로 늘립니다. 몇 시간짜리 작업도 로그가 몇 줄 늘 뿐입니다. |
| 금방 끝나는 작업의 잡음 | 첫 갱신 간격 안에 끝나면 진행 상황을 아예 남기지 않습니다. |
| 프롬프트에서 멈춤 | 터미널이 아니면 비밀번호도 삭제 확인도 묻지 않습니다. `s3-ops`의 삭제는 `--yes` 없이는 거부합니다. |
| 스택 트레이스로 가득한 메일 | 오류를 한 줄로 줄이고 종료 코드로 알립니다. 전체는 `--debug`로 봅니다. |

`PATH`는 크론에서 좁으므로 `python3`이 `/usr/bin` 밖에 있으면 지정하세요.

```cron
PYTHON=/opt/python3.11/bin/python3
```

종료 코드로 실패를 판별할 수 있습니다.

| 코드 | 뜻 | 쓰는 스크립트 |
| --- | --- | --- |
| `0` | 성공 | 전부 |
| `1` | 대상이 없거나 사용자가 취소함 | `s3-ops` |
| `2` | 인자가 잘못됨 (argparse) | 전부 |
| `3` | 필요한 파이썬 패키지 없음 | `impala-query`, `gp-query` |
| `4` | 접속 실패 | `impala-query`, `gp-query` |
| `5` | 쿼리·명령 실행 실패 | 전부 |

## 문서

올린 파일을 Greenplum에서 읽어들이는 쪽은 이 저장소의 코드가 아니라 Greenplum
설정입니다. 그 절차를 따로 정리해 두었습니다.

- [S3 외부 테이블로 읽기](docs/s3_external_table.md) — `s3.conf` 배포, 외부 테이블 작성, 파일 분할, 오류 허용
- [PXF로 S3 읽기 설정](docs/pxf.md) — `pxf-profiles.xml`, `s3-site.xml`, 외부 테이블 LOCATION
- [분산키 선정 가이드](docs/distribution_key.md) — 후보 컬럼 진단 쿼리
- [boto3로 S3 버킷·파일 목록 보기](docs/boto3.md) — 버킷 확인, 올린 파일 조회, 찌꺼기 정리

## 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

가짜 커서와 가짜 S3 클라이언트로 CSV 인코딩, 구분자 처리, 템플릿 변수, 표 정렬,
페이지네이션, 삭제 안전장치, 트랜잭션 커밋/롤백, 자격증명 우선순위, 크론 대응을
검증하므로 실제 Impala/Greenplum/S3 없이도 실행됩니다.

`bin/` 래퍼는 `LANG` 없이 `PATH`를 좁힌 채 무관한 디렉터리에서 실제로 실행해
확인합니다.

## 프로젝트 구조

```
src/
  appconfig.py         # conf/config.yaml 로딩, 환경변수 치환, 우선순위 규칙
  sqlfile.py           # sql/ 의 .sql 찾기, Jinja 템플릿 채우기
  progress.py          # 구간별 소요 시간, 진행 상황 (크론 대응)
  table.py             # -o 없을 때의 표 출력, 필요한 만큼만 받기
  impala_query.py      # Impala 쿼리 실행 → 표 또는 CSV (TLS + LDAP)
  gp_query.py          # Greenplum SQL 실행 → 표 또는 CSV
  s3_ops.py            # S3 업로드·삭제·디렉터리 생성/삭제·목록

bin/
  impala-query         # src/impala_query.py 실행 래퍼
  gp-query             # src/gp_query.py 실행 래퍼
  s3-ops               # src/s3_ops.py 실행 래퍼

sql/
  daily_orders.sql     # 하루치 주문 (--var dt)
  order_range.sql      # 기간별 집계 (--var from_dt, to_dt)
  order_summary.sql    # Greenplum 적재분 집계 (--var dt)
  README.md            # 템플릿 작성법

conf/
  config.yaml          # 기본으로 읽는 설정 파일 (sql.dir 로 위 경로를 바꿀 수 있음)
  config.example.yaml  # 모든 옵션을 주석과 함께 나열한 참조 문서
  config.local.yaml    # --config 로 지정해 쓰는 로컬 값 (.gitignore 대상)
  impala-ca.pem        # Impala TLS 검증용 CA 인증서 (직접 넣어야 함)
```

## Greenplum에 SQL 실행하기

`src/gp_query.py`가 `conf/config.yaml`의 `greenplum` 섹션으로 접속해 SQL을 실행합니다.
`sql/`의 템플릿과 `--var`는 `impala-query`와 똑같이 동작합니다.

```bash
export GP_PASSWORD='...'

# 결과가 있으면 표로 보여줍니다
bin/gp-query -f order_summary.sql --var dt=2026-08-01

# CSV로 저장
bin/gp-query -q "SELECT * FROM staging.orders" -o orders.csv

# DDL/DML. 성공하면 커밋합니다.
bin/gp-query -q "TRUNCATE staging.orders"
```

```
order_dt   | status   | order_cnt | amount_sum
-----------+----------+-----------+-----------
2026-08-01 | 결제완료 | 12        | 10.50
2026-08-01 | 취소     | NULL      | NULL
```

표는 stdout으로, 행 수와 소요 시간은 stderr로 나갑니다.

```
2행

=== 구간별 소요 시간 ===
  1. Greenplum 접속     0.081초   28.5%
  2. 쿼리 실행 요청     0.163초   57.4%
  3. 결과 수신          0.031초   10.9%
  ─────────────────────────────────
     합계              0.284초  100.0%
```

### 트랜잭션

**SELECT든 DDL이든 한 트랜잭션에서 실행하고 성공하면 커밋합니다.** 중간에 실패하면
전부 롤백되고 종료 코드 5로 끝납니다.

`--dry-run`은 실행은 하되 **항상 롤백합니다.** 지우는 문장이 무엇을 건드리는지
확인할 때 쓰세요.

```bash
bin/gp-query -f cleanup.sql --var dt=2026-08-01 --dry-run
# 1,204행
# --dry-run 이므로 롤백했습니다. 반영되지 않았습니다.
```

### 주요 옵션

| 옵션 | 설명 |
| --- | --- |
| `-o`, `--output` | 결과를 CSV로 저장합니다. 생략하면 표로 출력합니다. |
| `--max-rows` | 표로 출력할 최대 행 수 (기본 100). `0`이면 제한 없음. |
| `--schema` | 쿼리 전에 `search_path`로 지정할 스키마. 설정의 `greenplum.schema`. |
| `--session-sql` | 쿼리 전에 실행할 `SET` 문. 여러 번 지정 가능. |
| `--dry-run` | 실행 후 항상 롤백합니다. |
| `--debug` | 템플릿을 채운 뒤 실제로 보내는 SQL을 출력합니다. |
| `--no-password-prompt` | 비밀번호를 묻지 않습니다. `.pgpass`나 trust 인증을 쓸 때. |

행이 많으면 앞 100행만 보여주고 멈춥니다. **보여줄 만큼만 받고 끊기 때문에 총
개수는 알 수 없고 `100행 이상`으로 표시합니다.** 100행을 보려고 수백만 행을
받아오지 않기 위해서입니다. 정확한 개수가 필요하면 `--max-rows 0`으로 전부 받거나
`-o`로 파일에 받으세요.

비밀번호는 `--password` 같은 인자로 받지 않습니다. 설정의 `greenplum.password`
(보통 `${GP_PASSWORD}` 참조), 환경변수, 대화형 입력 순으로 찾습니다. 셋 다 없어도
접속을 시도합니다 — `.pgpass`나 trust 인증을 쓰는 환경이 있기 때문입니다.

## S3 파일 다루기

`src/s3_ops.py`로 업로드, 삭제, 디렉터리 생성/삭제를 할 수 있습니다.

```bash
bin/s3-ops ls     s3://dw-stage/impala/
bin/s3-ops upload orders.csv s3://dw-stage/impala/
bin/s3-ops upload ./out/ s3://dw-stage/impala/out/ --recursive
bin/s3-ops mkdir  s3://dw-stage/impala/2026-08-03/
bin/s3-ops rm     s3://dw-stage/impala/orders.csv --yes
bin/s3-ops rmdir  s3://dw-stage/impala/out/ --yes
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

### 접속 옵션

| 옵션 | 설명 |
| --- | --- |
| `-b`, `--bucket` | 기본 버킷. 주면 경로를 `s3://` 없이 키만 쓸 수 있습니다. |
| `--access-key` | AWS 액세스 키 |
| `--secret-key` | AWS 시크릿 키 |
| `--session-token` | 임시 자격증명(STS)의 세션 토큰 |
| `--region` | AWS 리전 |
| `--endpoint` | S3 호환 스토리지 엔드포인트 (MinIO 등) |
| `-c`, `--config` | 다른 설정 파일 지정 (기본 `conf/config.yaml`) |
| `--no-config` | 설정 파일을 읽지 않음 |

```bash
# 버킷을 미리 주면 키만 써도 됩니다
bin/s3-ops --bucket dw-stage ls impala/

# MinIO 등 S3 호환 스토리지
bin/s3-ops --endpoint http://minio:9000 --bucket dw-stage \
    --access-key minioadmin --secret-key minioadmin ls /
```

우선순위는 **명령행 > 설정 파일 > 환경변수/IAM 역할** 입니다. 설정에도 명령행에도
자격증명이 없으면 boto3 기본 자격증명 체인(`AWS_ACCESS_KEY_ID` 등, IAM 역할)이 그대로
동작합니다.

**시크릿 키를 명령행에 적으면 `ps`로 다른 사용자에게 보입니다.** 공용 서버에서는
`AWS_SECRET_ACCESS_KEY` 환경변수나 설정 파일의 `${AWS_SECRET_ACCESS_KEY}` 참조를
쓰세요.

## Impala에 쿼리 실행하기

`src/impala_query.py`가 TLS + LDAP 접속으로 조회해 **`-o`가 없으면 표로, 있으면
CSV 파일로** 내보냅니다. 어느 구간에 시간을 썼는지도 함께 보여줍니다.

접속 정보는 `conf/config.yaml`의 `impala` 섹션에서, 쿼리는 `sql/`의 `.sql` 템플릿에서
옵니다. `--host`, `-u/--user`, `--ca-cert`, `--auth-mechanism` 등으로 개별 값만 덮어쓸
수 있고, `--no-config`를 주면 설정을 아예 무시하고 명령행 인자만 씁니다.

```bash
pip install impyla pure-sasl thrift-sasl PyYAML Jinja2
export IMPALA_USER='etl_user'
export IMPALA_PASSWORD='...'

cp /경로/impala-ca.pem conf/impala-ca.pem   # 설정의 impala.ca_cert 가 가리키는 위치

# 표로 확인
bin/impala-query -f daily_orders.sql -V dt=2026-08-01

# CSV 파일로 저장
bin/impala-query -f daily_orders.sql -V dt=2026-08-01 -o orders.csv.gz --gzip

# 설정 없이 전부 명령행으로
bin/impala-query --no-config \
    --host impala.example.com --user etl_user \
    --ca-cert /etc/ssl/certs/impala-ca.pem \
    -q "SELECT 1" -o out.csv
```

**`gp-query`와 같은 규칙입니다.** 표는 기본 100행까지 보여주고 `--max-rows`로
조절하며, 보여줄 만큼만 받고 멈추므로 총 개수 대신 `100행 이상`으로 표시합니다.

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
  때문에 설정 파일(`impala.password`, 보통 `${IMPALA_PASSWORD}` 참조), 환경변수,
  대화형 입력 순으로만 받습니다.
- CA 인증서는 `conf/impala-ca.pem`을 씁니다. 설정의 `impala.ca_cert`에 적은 상대
  경로는 **작업 디렉터리가 아니라 설정 파일이 있는 `conf/` 기준**으로 풀리므로,
  어느 디렉터리에서 실행해도 같은 파일을 가리킵니다. 절대 경로도 그대로 쓸 수
  있습니다.
- 인증서가 그 경로에 없으면 접속을 시도하기 전에 경로를 짚어 알려줍니다. 조용히
  넘어가면 검증 없이 접속하게 되기 때문입니다. 검증이 필요 없다면
  `impala.ca_cert`를 비우세요. 그러면 통신은 암호화되지만 서버 인증서는 확인하지
  않습니다.
- 엑셀에서 한글이 깨지면 `--encoding utf-8-sig`를 쓰세요.
- 그 밖에 `--gzip`, `--null-string`, `--query-file`, `--var KEY=VALUE`,
  `--set KEY=VALUE`, `--max-rows`, `--no-progress`를 지원합니다. 자세한 건
  `--help`를 보세요.

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
bin/impala-query ... --quote

# 쉼표 구분으로 되돌리기
bin/impala-query ... --delimiter ,
```

값 안에 구분자가 들어 있으면 이스케이프됩니다(`백틱` → `` 백틱\`포함 ``). `--escapechar ''`
로 끌 수 있지만, 그 상태에서 값에 구분자가 나오면 **오류로 중단**됩니다. 이스케이프
문자가 들어간 값은 한 번 더 이스케이프되므로(`\N` → `\\N`), `--null-string '\N'`처럼
쓸 때는 `--escapechar ''`를 함께 고려하세요.

따옴표를 쓰지 않으면 값 안의 **줄바꿈이 레코드를 깨뜨립니다.** 줄바꿈이 들어갈 수 있는
컬럼이라면 `--quote`를 켜거나 쿼리에서 `regexp_replace`로 걷어내세요.

### SQL 파일과 템플릿 변수

여러 줄짜리 쿼리는 `sql/` 아래에 `.sql` 파일로 두고 이름만 넘깁니다.

```bash
bin/impala-query -f daily_orders.sql --var dt=2026-08-01 -o orders.csv
```

이름에 경로 구분자가 없으면 `sql/`에서 찾고, 없는 이름을 주면 거기 있는 파일을
나열해 줍니다. 다른 위치의 파일은 경로를 그대로 주면 됩니다.

찾을 디렉터리는 바꿀 수 있습니다. `conf/config.yaml`의 `sql.dir`에 적거나
`--sql-dir`로 그때만 지정합니다.

```yaml
sql:
  dir: ../sql            # conf/ 기준 상대 경로. 절대 경로도 됩니다.
```

```bash
bin/impala-query --sql-dir /srv/etl/queries -f daily_orders.sql -V dt=2026-08-01 -o out.csv
```

설정의 `sql.dir`은 `ca_cert`와 같이 **설정 파일이 있는 디렉터리 기준**으로 풀리고,
명령행 `--sql-dir`은 셸에서 친 경로라 작업 디렉터리 기준입니다.

`.sql` 파일은 **Jinja 템플릿**입니다. `{{ 변수 }}` 자리에 `-V`/`--var KEY=VALUE`로 준
값이 들어가고, 여러 번 지정할 수 있습니다.

```sql
-- sql/daily_orders.sql
SELECT order_id, customer_id, amount
  FROM sales.orders
 WHERE order_dt = '{{ dt }}'
{%- if status %}
   AND status = '{{ status }}'
{%- endif %}
```

```bash
bin/impala-query -f daily_orders.sql \
    --var dt=2026-08-01 --var status=PAID -o orders.csv
```

| 동작 | 설명 |
| --- | --- |
| `{{ var }}` | `--var`로 준 값이 들어갑니다. **안 주면 오류입니다.** |
| `{% if var %}` | 안 주면 거짓으로 보고 그 블록을 건너뜁니다. 선택적 필터에 씁니다. |
| `{{ var \| default("x") }}` | 안 줬을 때 쓸 기본값을 템플릿에 적습니다. |

`{{ }}`로 참조한 변수를 안 줬을 때 오류로 처리하는 이유는, Jinja 기본 동작인 빈
문자열 치환이 SQL에서는 `WHERE order_dt = ''` 같은 문장을 조용히 만들어 0건을
돌려주기 때문입니다. 오타를 그 자리에서 잡는 편이 낫습니다.

```
sql/daily_orders.sql 의 템플릿 변수를 채우지 못했습니다: 'dt' is undefined
  지금 준 변수: (없음)
  --var KEY=VALUE 로 지정하세요.
```

**따옴표는 템플릿이 책임집니다.** 값은 SQL에 그대로 들어가므로 위 예제처럼
`'{{ dt }}'`로 따옴표를 템플릿 쪽에 둡니다. 다른 시스템에서 받은 값을 넘긴다면
넘기기 전에 형식을 검증하세요.

`{{ }}`도 `{% %}`도 없는 파일은 Jinja를 거치지 않고 그대로 나갑니다. 평범한 SQL만
쓴다면 Jinja2를 설치하지 않아도 됩니다.

**파일 내용은 템플릿을 채운 뒤 그대로 실행합니다.** 문장을 쪼개거나 세미콜론을
떼어내지 않으므로, 여러 줄로 이어진 쿼리도 주석도 작성한 그대로 서버에 전달됩니다.

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
bin/impala-query --host impala.example.com --user etl_user \
    --port 28000 --http-transport --ca-cert /etc/ssl/certs/impala-ca.pem \
    -q "SELECT 1" -o test.csv

# 서버가 평문이라면
bin/impala-query ... --no-ssl

# 인증이 없는 서버라면
bin/impala-query ... --auth-mechanism NOSASL
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
