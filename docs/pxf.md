# PXF로 S3 읽기 설정

`s3.protocol: pxf` 를 쓰면 Greenplum 내장 `s3` 프로토콜 대신 PXF를 통해 S3를 읽습니다.
이 문서는 그때 필요한 PXF 쪽 설정을 다룹니다.

## 어떤 파일에 무엇을 적는가

먼저 헷갈리기 쉬운 부분부터 정리합니다. **S3 경로(버킷·디렉터리)는
`pxf-profiles.xml` 에 적지 않습니다.** 세 곳의 역할이 다릅니다.

| 파일 / 위치 | 무엇을 적는가 |
| --- | --- |
| `$PXF_BASE/conf/pxf-profiles.xml` | **프로파일 정의.** 이름 하나에 플러그인 클래스와 기본 옵션을 묶습니다. |
| `$PXF_BASE/servers/<서버>/s3-site.xml` | **접속 정보.** 액세스 키, 시크릿, 엔드포인트, 리전. |
| 외부 테이블 `LOCATION` | **실제 S3 경로.** `pxf://버킷/디렉터리/?PROFILE=...&SERVER=...` |

즉 "S3 경로를 추가한다"는 건 대부분 **외부 테이블을 만드는 일**이고,
`pxf-profiles.xml` 은 그 경로를 어떤 방식으로 읽을지(포맷·압축 등)를 정의합니다.

버킷마다 자격증명이 다르면 `servers/` 아래에 서버 디렉터리를 하나씩 만들고,
읽는 방식이 다르면 프로파일을 하나씩 추가하는 식으로 나눠 관리합니다.

## SERVER= 에는 무엇이 들어가는가

`$PXF_BASE/servers/` 아래에 만든 **디렉터리 이름**이 그대로 들어갑니다.
직접 정하는 임의의 이름입니다.

```
$PXF_BASE/servers/s3srv/s3-site.xml        ← 이 디렉터리를 만들면
                  ^^^^^
LOCATION ('pxf://dw-stage/data/?PROFILE=s3:text&SERVER=s3srv')
                                                        ^^^^^  ← 이 이름을 쓴다
```

이름 자체에는 아무 의미가 없습니다. `s3srv`, `prod-s3`, `dw` 무엇이든 되고,
PXF는 그 이름의 디렉터리에서 `s3-site.xml` 을 찾을 뿐입니다.

**호스트명·버킷명·엔드포인트가 아닙니다.** 이 셋과 헷갈리기 쉬운데, 그런 값은
전부 다른 곳에 적습니다.

| 넣고 싶은 것 | 실제로 적는 곳 |
| --- | --- |
| S3 호스트/엔드포인트 | `s3-site.xml` 의 `fs.s3a.endpoint` |
| 버킷 이름 | LOCATION의 `pxf://` 바로 뒤 |
| 자격증명 | `s3-site.xml` 의 `fs.s3a.access.key` / `secret.key` |
| 서버 설정 디렉터리 이름 | `SERVER=` ← 여기 |

### 생략하면

`SERVER=` 를 빼면 `default` 서버를 씁니다. 즉 `$PXF_BASE/servers/default/` 의
설정을 읽습니다.

```sql
-- 아래 둘은 같은 뜻이다
LOCATION ('pxf://dw-stage/data/?PROFILE=s3:text')
LOCATION ('pxf://dw-stage/data/?PROFILE=s3:text&SERVER=default')
```

S3만 쓰는 환경이면 `default` 에 `s3-site.xml` 을 두고 `SERVER=` 를 생략해도 됩니다.
다만 HDFS 등 다른 소스와 섞어 쓴다면 이름을 나눠두는 편이 헷갈리지 않습니다.

### 확인하는 법

지금 쓸 수 있는 서버 이름은 디렉터리 목록이 곧 답입니다.

```bash
ls "$PXF_BASE/servers/"
# default  s3srv  hdfs-prod
```

이름을 지을 때 주의할 점:

- **소문자로 두세요.** PXF는 서버 이름을 소문자로 처리하므로 디렉터리를 `S3Srv` 로
  만들면 찾지 못할 수 있습니다.
- 디렉터리는 **모든 PXF 호스트에** 있어야 합니다. 마스터에서 만든 뒤 반드시
  `pxf cluster sync` 를 실행하세요. 빠뜨리면 일부 세그먼트에서만
  `server configuration not found` 류의 오류가 납니다.

이 프로젝트에서는 설정의 `s3.pxf_server` 값이 그대로 `SERVER=` 로 들어갑니다.

```yaml
s3:
  protocol: pxf
  pxf_server: s3srv     # → ...&SERVER=s3srv
```

## 1. 데이터베이스에 pxf 확장 설치

**가장 먼저 할 일입니다.** `pxf` 는 Greenplum에 내장된 프로토콜이 아니라 확장으로
등록해야 하는 사용자 정의 프로토콜입니다. 이 단계를 건너뛰면 외부 테이블을 만들 때
이렇게 실패합니다.

```
ERROR:  protocol "pxf" does not exist
```

내장 `s3` 프로토콜은 별도 등록 없이 바로 쓸 수 있어서, 그쪽만 써봤다면 이 단계가
필요하다는 걸 놓치기 쉽습니다.

```sql
-- 데이터를 적재할 데이터베이스에 접속해서 실행한다
\c dw
CREATE EXTENSION pxf;
```

**확장은 데이터베이스마다 따로 설치해야 합니다.** `postgres` 에 설치했다고 `dw` 에서
쓸 수 있는 게 아닙니다. 같은 실수를 반복하기 쉬운 지점입니다.

설치되었는지 확인:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'pxf';
SELECT ptcname FROM pg_extprotocol;      -- pxf 가 보여야 한다
```

### CREATE EXTENSION 자체가 실패한다면

```
ERROR:  could not open extension control file ".../pxf.control": No such file or directory
```

확장 파일이 `$GPHOME` 에 아직 복사되지 않은 상태입니다. PXF 6 이상에서는
`register` 로 설치합니다.

```bash
pxf cluster register    # $GPHOME/share/postgresql/extension/ 에 확장 파일 배포
pxf cluster restart
```

그다음 다시 `CREATE EXTENSION pxf;` 를 실행합니다.

### 일반 사용자에게 권한 주기

`CREATE EXTENSION` 은 슈퍼유저로 실행해야 하고, 슈퍼유저가 아닌 계정이 외부
테이블을 만들려면 프로토콜 권한이 따로 필요합니다.

```sql
GRANT SELECT ON PROTOCOL pxf TO etl;    -- 읽기 전용 외부 테이블
GRANT INSERT ON PROTOCOL pxf TO etl;    -- 쓰기 가능 외부 테이블
```

권한이 없으면 `permission denied for protocol pxf` 가 납니다.
`protocol "pxf" does not exist` 와는 다른 오류이므로 메시지로 구분하세요.

## 2. pxf-profiles.xml — 대부분 건드릴 필요가 없습니다

`s3:text`, `s3:parquet` 같은 기본 프로파일은 이미 정의되어 있습니다. 이 프로젝트가
올리는 탭 구분 gzip TSV도 기본 `s3:text` 로 그대로 읽히므로, **먼저 커스텀 프로파일
없이 되는지 확인하세요.** 포맷 옵션은 외부 테이블 쪽에 적으면 됩니다.

```sql
CREATE EXTERNAL TABLE staging.ext_orders (...)
LOCATION ('pxf://dw-stage/impala-to-greenplum/orders-9f2c/?PROFILE=s3:text&SERVER=s3srv')
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N');
```

`pxf-profiles.xml` 을 잘못 쓰면 PXF가 아예 뜨지 않고 시작 단계에서
`ProfileConf` 초기화 NPE로 죽습니다. 아래 [ProfileConf 초기화 NPE](#profileconf-초기화-npe)
를 참고하세요. 얻는 것에 비해 위험이 큰 편이라, 같은 옵션을 반복해서 쓰는 게
정말 번거로울 때만 손대는 편이 낫습니다.

### 그래도 프로파일을 추가한다면

`$PXF_BASE/conf/pxf-profiles.xml` 을 편집합니다. 이 파일은 기본 정의를 덮어쓰는
용도이고, 여기에 없는 프로파일은 `pxf-profiles-default.xml` 의 정의가 쓰입니다.

**직접 타이핑하지 말고 기본 정의를 통째로 복사한 뒤 이름만 바꾸세요.**
`<plugins>` 안의 클래스 이름과 함께 있어야 하는 요소가 PXF 버전마다 다릅니다.

```bash
# 설치본에서 s3:text 정의를 그대로 꺼내온다
sed -n '/<name>s3:text<\/name>/,/<\/profile>/p' \
    "$PXF_HOME/conf/pxf-profiles-default.xml"
```

꺼낸 `<profile>` 블록을 `<profiles>` 안에 붙여넣고, `<name>` 만 바꾼 뒤 필요한
`<optionMappings>` 를 더합니다.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<profiles>
    <profile>
        <!-- name 은 반드시 있어야 한다. 비거나 없으면 시작 시 NPE 가 난다 -->
        <name>s3:impala-staging</name>

        <!-- 여기부터는 pxf-profiles-default.xml 의 s3:text 블록을 그대로 복사한 것.
             plugins/protocol/handler 구성은 버전마다 다르므로 임의로 지우거나
             더하지 말고 통째로 옮길 것. -->
        <plugins>
            <fragmenter>...설치본에서 복사...</fragmenter>
            <accessor>...설치본에서 복사...</accessor>
            <resolver>...설치본에서 복사...</resolver>
        </plugins>

        <optionMappings>
            <mapping>
                <option>COMPRESSION_CODEC</option>
                <property>compression.codec</property>
            </mapping>
        </optionMappings>
    </profile>
</profiles>
```

없는 클래스를 적으면 조회 시점에 `ClassNotFoundException` 이 납니다.

편집한 뒤에는 **반드시 전 노드에 배포하고 재시작**해야 반영됩니다.

```bash
pxf cluster sync      # 마스터의 $PXF_BASE 설정을 전 세그먼트 호스트로 복사
pxf cluster restart   # 프로파일 변경은 재시작해야 적용된다
```

`sync` 를 빠뜨리면 마스터에서만 바뀌고 세그먼트는 예전 정의를 쓰기 때문에,
"어떤 세그먼트에서만 실패"하는 형태로 증상이 나타납니다.

## 3. s3-site.xml 에 접속 정보 추가

서버 디렉터리를 만들고 템플릿을 복사한 뒤 자격증명을 채웁니다. 여기서 정한
디렉터리 이름이 곧 `LOCATION` 의 `SERVER=` 값입니다(위 [SERVER= 절](#server-에는-무엇이-들어가는가) 참고).

```bash
mkdir -p "$PXF_BASE/servers/s3srv"
cp "$PXF_HOME/templates/s3-site.xml" "$PXF_BASE/servers/s3srv/"
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <property>
        <name>fs.s3a.access.key</name>
        <value>AKIA...</value>
    </property>
    <property>
        <name>fs.s3a.secret.key</name>
        <value>secret...</value>
    </property>
    <property>
        <name>fs.s3a.endpoint</name>
        <value>s3.ap-northeast-2.amazonaws.com</value>
    </property>
    <!-- MinIO 등 S3 호환 스토리지라면 경로 방식 접근이 필요할 수 있다
    <property>
        <name>fs.s3a.path.style.access</name>
        <value>true</value>
    </property>
    -->
</configuration>
```

자격증명이 평문으로 들어가므로 권한을 좁히세요.

```bash
chmod 600 "$PXF_BASE/servers/s3srv/s3-site.xml"
pxf cluster sync
```

EC2에서 IAM 역할을 쓴다면 키를 비우고 아래처럼 인스턴스 프로파일을 쓸 수 있습니다.

```xml
<property>
    <name>fs.s3a.aws.credentials.provider</name>
    <value>com.amazonaws.auth.InstanceProfileCredentialsProvider</value>
</property>
```

버킷마다 자격증명이 다르면 `servers/s3-prod`, `servers/s3-dev` 처럼 디렉터리를
나누고 `SERVER=` 로 골라 쓰면 됩니다.

## 4. S3 경로를 가리키는 외부 테이블

여기가 실제로 "S3 경로를 추가"하는 곳입니다.

```sql
CREATE EXTERNAL TABLE staging.ext_orders (
    order_id  bigint,
    name      text,
    amount    numeric(18,2)
)
LOCATION ('pxf://dw-stage/impala-to-greenplum/orders-9f2c/?PROFILE=s3:text&SERVER=s3srv')
FORMAT 'TEXT' (DELIMITER E'\t' NULL E'\\N');

INSERT INTO staging.orders SELECT * FROM staging.ext_orders;
```

LOCATION을 뜯어보면 이렇습니다.

```
pxf://dw-stage/impala-to-greenplum/orders-9f2c/?PROFILE=s3:text&SERVER=s3srv
      └ 버킷 ┘└──────── 접두사(디렉터리) ─────┘  └ 2번 프로파일 ┘└ 3번 서버 ┘
```

- 접두사는 디렉터리처럼 동작합니다. 그 아래 파일을 세그먼트가 나눠 읽습니다.
- `.gz` 파일은 확장자를 보고 알아서 풀어 읽습니다.
- 2번에서 만든 프로파일을 쓰려면 `PROFILE=s3:impala-staging` 으로 바꿉니다.

## 5. 이 프로젝트에서 쓰기

설정에서 `protocol: pxf` 로 바꾸면 파이프라인이 위 형태의 LOCATION을 만들어 줍니다.

```yaml
s3:
  bucket: dw-stage
  prefix: impala-to-greenplum
  protocol: pxf
  pxf_server: s3srv          # 3번에서 만든 서버 디렉터리 이름
  file_size_mb: 128
```

만들어지는 LOCATION은 이렇습니다.

```
pxf://dw-stage/impala-to-greenplum/orders-{난수}/?PROFILE=s3:text&SERVER=s3srv
```

`endpoint` 나 `gp_config` 는 PXF 모드에서 쓰이지 않습니다. 접속 정보가 전부
`s3-site.xml` 에 있기 때문입니다. boto3 업로드용 자격증명(`access_key_id` 등)은
그대로 필요합니다. **파이썬이 올릴 때 쓰는 자격증명과 PXF가 읽을 때 쓰는
자격증명은 별개**라, 두 곳 모두 같은 버킷에 접근할 수 있어야 합니다.

## 6. 확인과 문제 해결

설정이 반영됐는지 순서대로 확인합니다.

```bash
pxf cluster status                    # 전 노드에서 떠 있는지
pxf cluster sync                      # 설정 배포
pxf cluster restart                   # 프로파일·서버 변경 후
```

```sql
-- 가장 작은 단위로 먼저 확인
CREATE EXTERNAL TABLE ext_probe (line text)
LOCATION ('pxf://dw-stage/impala-to-greenplum/?PROFILE=s3:text&SERVER=s3srv')
FORMAT 'TEXT' (DELIMITER E'\t');

SELECT * FROM ext_probe LIMIT 5;
```

| 증상 | 확인할 것 |
| --- | --- |
| `protocol "pxf" does not exist` | 그 데이터베이스에 `CREATE EXTENSION pxf;` 를 하지 않았습니다. [1번 절](#1-데이터베이스에-pxf-확장-설치) |
| `permission denied for protocol pxf` | 확장은 있지만 권한이 없습니다. `GRANT SELECT ON PROTOCOL pxf TO ...` |
| `could not open extension control file` | `pxf cluster register` 로 확장 파일을 배포하세요. |
| 시작 시 `ProfileConf` NPE | `pxf-profiles.xml` 구조. 아래 절 참고. |
| `ClassNotFoundException` | `pxf-profiles.xml` 의 플러그인 클래스 이름. 설치본 기본 정의와 대조하세요. |
| `Profile ... is not defined` | 프로파일 오타이거나 `pxf cluster sync` 를 빠뜨렸습니다. |
| `Failed to connect to ... 5888` | 해당 세그먼트 호스트의 PXF가 죽어 있습니다. `pxf cluster status` |
| `AccessDenied` / `403` | `s3-site.xml` 의 키 또는 버킷 정책. 업로드용 자격증명과 별개입니다. |
| 일부 세그먼트에서만 실패 | `pxf cluster sync` 누락. 마스터만 바뀐 상태입니다. |
| 결과가 비어 있음 | LOCATION 접두사에 파일이 있는지 확인하세요(`examples/s3_ops.py ls`). |

로그는 각 세그먼트 호스트의 `$PXF_LOGDIR/pxf-service.log` 에 쌓입니다. 실패한
호스트에서 직접 열어보는 게 가장 빠릅니다.

## ProfileConf 초기화 NPE

`ProfileConf` 는 PXF가 시작하면서 `pxf-profiles-default.xml` 과
`pxf-profiles.xml` 을 읽어 프로파일 목록을 만드는 클래스입니다. 여기서
`NullPointerException` 이 나면 **XML 문법은 맞지만 필수 요소가 빠졌다**는 뜻입니다.
문법이 깨졌다면 파싱 오류가 났을 테니까요.

### 원인 후보 (흔한 순서)

**1. `<profile>` 안에 `<name>` 이 없거나 비어 있음** — 가장 흔합니다.

PXF는 프로파일 이름을 소문자로 바꿔 맵의 키로 씁니다. 이름이 없으면 그 시점에
바로 NPE가 납니다. 아래는 전부 같은 결과를 냅니다.

```xml
<!-- 이름이 없다 -->
<profile>
    <plugins>...</plugins>
</profile>

<!-- 비어 있다 -->
<profile>
    <name></name>
    <plugins>...</plugins>
</profile>

<!-- 태그 이름 오타 -->
<profile>
    <n>s3:my</n>
    <plugins>...</plugins>
</profile>
```

주석을 지우다가 빈 `<profile/>` 하나가 남는 경우도 같은 증상입니다.

**2. 루트 요소가 `<profiles>` 가 아님** — `<configuration>` 이나 다른 이름으로
감싸면 프로파일을 찾지 못합니다.

**3. `<plugins>` 블록이 통째로 없음** — 이름만 있고 플러그인 정의가 없으면
플러그인 맵을 만드는 단계에서 NPE가 날 수 있습니다.

**4. 요소 중첩이 어긋남** — `<plugins>` 가 `<profile>` 밖에 있거나, `<mapping>` 이
`<optionMappings>` 밖에 있는 경우.

### 좁히는 순서

빈 파일로 바꿔 시작되는지부터 확인하면 원인이 파일 안에 있는지 밖에 있는지
바로 갈립니다.

```bash
# 1) 현재 파일을 백업하고 빈 정의로 교체
cp "$PXF_BASE/conf/pxf-profiles.xml" /tmp/pxf-profiles.xml.bak
cat > "$PXF_BASE/conf/pxf-profiles.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<profiles>
</profiles>
XML

pxf cluster sync && pxf cluster restart
pxf cluster status
```

여기서 정상적으로 뜨면 원인은 확실히 그 파일 안에 있습니다. 백업한 파일에서
프로파일을 하나씩 되살리며 어느 것이 문제인지 좁힙니다.

```bash
# 2) 문법과 구조 확인
xmllint --noout /tmp/pxf-profiles.xml.bak        # 문법
xmllint --xpath 'count(//profile)' /tmp/pxf-profiles.xml.bak      # 프로파일 개수
xmllint --xpath 'count(//profile[not(name) or name=""])' \
        /tmp/pxf-profiles.xml.bak                # 이름 없는 프로파일 개수 → 0이어야 한다
```

마지막 명령이 0이 아니면 그게 원인입니다.

```bash
# 3) 전체 스택 트레이스 확인
grep -n -A 30 "ProfileConf" "$PXF_LOGDIR/pxf-service.log" | head -50
```

NPE가 난 줄 번호를 보면 이름 처리 단계인지 플러그인 처리 단계인지 갈립니다.

### 되돌리기

원인을 찾기 전이라도 서비스는 바로 살릴 수 있습니다. `pxf-profiles.xml` 을 비우면
기본 정의(`pxf-profiles-default.xml`)만 쓰게 되고, `s3:text` 같은 기본 프로파일은
그대로 동작합니다.

```bash
cat > "$PXF_BASE/conf/pxf-profiles.xml" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<profiles>
</profiles>
XML
pxf cluster sync && pxf cluster restart
```

`pxf-profiles-default.xml` 은 **직접 고치지 마세요.** 업그레이드 때 덮어써지고,
잘못 고치면 모든 프로파일이 함께 죽습니다.

## 내장 s3 프로토콜과 비교

| | PXF | 내장 `s3` 프로토콜 |
| --- | --- | --- |
| 설정 위치 | `s3-site.xml`(XML) | `s3.conf`(INI) |
| 배포 | `pxf cluster sync` | `gpscp` 로 직접 복사 |
| 별도 서비스 | 세그먼트마다 JVM 프로세스 | 없음 |
| 포맷 | text, parquet, avro, orc 등 | text, csv |

PXF가 이미 구축되어 있으면 그대로 쓰고, 아니면 설정이 간단한 내장 `s3` 프로토콜이
낫습니다. 그쪽 절차는 [S3 외부 테이블 적재 설정](s3_external_table.md)에 있습니다.
