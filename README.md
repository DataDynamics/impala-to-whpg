# impala-to-whpg

Impala 조회와 S3 조작을 위한 명령행 도구 모음입니다.

| 도구 | 하는 일 |
| --- | --- |
| `bin/query-to-csv` | Impala에서 쿼리를 실행해 CSV로 저장하고 구간별 소요 시간을 보여줍니다. |
| `bin/s3-ops` | S3 업로드·삭제·디렉터리 생성/삭제·목록. |

pip로 설치하지 않고 저장소에서 바로 실행합니다. `bin/` 아래 스크립트가 소스 위치를
찾아 파이썬에 넘겨주므로 어느 디렉터리에서 호출해도 동작합니다. 다른 인터프리터를
쓰려면 `PYTHON=/path/to/python bin/s3-ops ...` 처럼 지정하세요.

## 설치

```bash
pip install -r requirements.txt
```

두 도구가 필요로 하는 것이 다릅니다. 쓰지 않는 쪽은 설치하지 않아도 됩니다.

| 도구 | 필요한 패키지 |
| --- | --- |
| `query-to-csv` | `impyla` (+ LDAP/Kerberos 인증 시 `pure-sasl`, `thrift-sasl`) |
| `s3-ops` | `boto3` (+ `--config` 를 쓸 때만 `PyYAML`) |

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

```bash
# Impala 쿼리 결과를 CSV로
export IMPALA_PASSWORD='...'
bin/query-to-csv --host impala.example.com --user etl_user \
    --ca-cert /etc/ssl/certs/impala-ca.pem \
    --query "SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'" \
    --output orders.csv

# S3 목록 확인
bin/s3-ops ls s3://dw-stage/impala/
```

`s3-ops`의 접속 정보는 매번 인자로 주는 대신 설정 파일에 담아둘 수 있습니다.

```bash
cp conf/config.yaml conf/config.local.yaml
bin/s3-ops --config conf/config.local.yaml ls s3://dw-stage/
```

`query-to-csv`는 설정 파일을 읽지 않고 인자로만 동작합니다. **이 파일 하나만 다른
곳으로 복사해서 써도 되도록** 표준 라이브러리와 impyla 외에는 아무것도 쓰지 않습니다.

### 설정 파일 세 개의 역할

| 파일 | 용도 |
| --- | --- |
| `conf/config.yaml` | 바로 돌려볼 수 있는 최소 예제. 저장소에 커밋됩니다. |
| `conf/config.example.yaml` | 쓸 수 있는 모든 옵션을 주석과 함께 나열한 참조 문서. |
| `conf/config.local.yaml` | 실제 운영 값. `.gitignore`에 걸려 있어 커밋되지 않습니다. |

**운영 값은 반드시 `conf/config.local.yaml`에 두세요.** `conf/config.yaml`은 커밋되는 파일이라
여기에 실제 호스트나 비밀번호를 적으면 저장소에 그대로 올라갑니다.

비밀번호 같은 민감한 값은 어느 파일에서든 YAML에 직접 쓰지 말고 `${AWS_SECRET_ACCESS_KEY}`
또는 `${AWS_DEFAULT_REGION:-ap-northeast-2}` 형태로 환경변수를 참조하세요. 정의되지 않은
환경변수를 기본값 없이 참조하면 실행 시점에 바로 오류가 나므로, 값이 비어 있는 채로
접속을 시도하는 일은 없습니다.

## 문서

- [S3 외부 테이블 적재 설정](docs/s3_external_table.md) — `s3.conf` 배포, 파일 분할, 오류 허용
- [PXF로 S3 읽기 설정](docs/pxf.md) — `pxf-profiles.xml`, `s3-site.xml`, 외부 테이블 LOCATION
- [분산키 선정 가이드](docs/distribution_key.md) — 후보 컬럼 진단 쿼리
- [boto3로 S3 버킷·파일 목록 보기](docs/boto3.md) — 버킷 확인, 스테이징 파일 조회, 찌꺼기 정리

## 테스트

```bash
pip install pytest
python -m pytest tests/ -v
```

가짜 Impala 커서와 가짜 S3 클라이언트로 CSV 인코딩, 구분자 처리, 페이지네이션,
삭제 안전장치, 자격증명 우선순위를 검증하므로 실제 Impala/S3 없이도 실행됩니다.

## 프로젝트 구조

```
src/
  query_to_csv.py       # Impala 쿼리 → CSV 저장 (TLS + LDAP, 구간별 시간 측정)
  s3_ops.py             # S3 업로드·삭제·디렉터리 생성/삭제·목록

bin/
  query-to-csv          # src/query_to_csv.py 실행 래퍼
  s3-ops                # src/s3_ops.py 실행 래퍼

conf/
  config.yaml           # 바로 돌려볼 수 있는 최소 예제
  config.example.yaml   # 모든 옵션을 주석과 함께 나열한 참조 문서
  config.local.yaml     # 실제 운영 값 (.gitignore 대상)
```

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
| `-c`, `--config` | 프로젝트 설정 파일의 `s3` 섹션 재사용 |

```bash
# 버킷을 미리 주면 키만 써도 됩니다
bin/s3-ops --bucket dw-stage ls impala/

# MinIO 등 S3 호환 스토리지
bin/s3-ops --endpoint http://minio:9000 --bucket dw-stage \
    --access-key minioadmin --secret-key minioadmin ls /
```

우선순위는 **명령행 > 설정 파일 > 환경변수/IAM 역할** 입니다. 아무것도 주지 않으면
boto3 기본 자격증명 체인(`AWS_ACCESS_KEY_ID` 등, IAM 역할)이 그대로 동작합니다.

**시크릿 키를 명령행에 적으면 `ps`로 다른 사용자에게 보입니다.** 공용 서버에서는
`AWS_SECRET_ACCESS_KEY` 환경변수나 `--config`를 쓰세요.

## Impala 쿼리를 CSV로 내려받기

Greenplum 적재와 별개로, Impala 결과를 파일로 뽑아야 할 때가 있습니다.
`src/query_to_csv.py`가 TLS + LDAP 접속으로 조회해 CSV로 저장하고, 어느 구간에
시간을 썼는지 보여줍니다.

**이 스크립트는 단독으로 동작합니다.** 표준 라이브러리와 impyla 외에 아무것도
필요하지 않으므로, 이 파일 하나만 복사해서 다른 곳에서 써도 됩니다.

```bash
pip install impyla pure-sasl thrift-sasl
export IMPALA_PASSWORD='...'

bin/query-to-csv \
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
bin/query-to-csv ... --quote

# 쉼표 구분으로 되돌리기
bin/query-to-csv ... --delimiter ,
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
bin/query-to-csv --host ... --user ... \
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
bin/query-to-csv --host impala.example.com --user etl_user \
    --port 28000 --http-transport --ca-cert /etc/ssl/certs/impala-ca.pem \
    -q "SELECT 1" -o test.csv

# 서버가 평문이라면
bin/query-to-csv ... --no-ssl

# 인증이 없는 서버라면
bin/query-to-csv ... --auth-mechanism NOSASL
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
