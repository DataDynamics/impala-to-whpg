# boto3로 S3 버킷·파일 목록 보기

쓰려는 버킷에 접근이 되는지 확인하거나, 올린 파일을 들여다보거나, 지우지 않고
남은 찌꺼기를 찾을 때 쓰는 예제입니다.

**아래 내용 대부분은 `bin/s3-ops` 로 바로 됩니다.** 이 문서는 그 스크립트가 안에서
무엇을 하고 있는지, 그리고 스크립트에 없는 것을 직접 짤 때 무엇을 조심해야 하는지를
다룹니다.

| 하려는 일 | 명령 |
| --- | --- |
| 접두사 아래 파일 나열 (2절) | `bin/s3-ops ls s3://버킷/접두사/` |
| 개수와 크기 분포 (3절) | `bin/s3-ops ls ... --summary` |
| 디렉터리만 묶어 보기 (4절) | `bin/s3-ops ls ... --dirs` |
| 오래 남은 찌꺼기 (5절) | `bin/s3-ops ls ... --older-than 24h` |
| 파일 내용 확인 (6절) | `bin/s3-ops head s3://버킷/키` |
| 파일 하나 메타데이터 (7절) | `bin/s3-ops exists s3://버킷/키` |
| 버킷 목록 (1절) | `bin/s3-ops buckets --show-region` |
| 정리 | `bin/s3-ops rmdir ... --older-than 7d --yes` |

## 준비

```bash
pip install boto3
```

자격증명은 아래 중 하나로 잡히면 됩니다. 코드에 직접 키를 박지 마세요.

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-northeast-2
```

EC2/EKS에서 IAM 역할을 쓴다면 환경변수 없이도 자동으로 잡힙니다.

## 1. 버킷 목록 보기

계정에 어떤 버킷이 있는지부터 확인합니다. 설정에 적은 버킷 이름이 맞는지, 접근
권한이 제대로 붙었는지 점검할 때 가장 먼저 돌려보는 코드입니다.

```python
import boto3

s3 = boto3.client("s3")

response = s3.list_buckets()
for bucket in response["Buckets"]:
    print(f"{bucket['Name']}\t{bucket['CreationDate']:%Y-%m-%d}")

owner = response.get("Owner", {})
print(f"\n총 {len(response['Buckets'])}개 (소유자: {owner.get('DisplayName', owner.get('ID'))})")
```

```
dw-stage	2025-11-02
dw-archive	2024-06-18
etl-logs	2024-06-18

총 3개 (소유자: data-platform)
```

`list_buckets` 는 **리전과 무관하게 계정의 모든 버킷** 을 돌려줍니다. 클라이언트를
만들 때 지정한 리전은 여기에 영향을 주지 않습니다.

### 리전까지 함께 보기

버킷마다 리전이 다를 수 있고, Greenplum의 `s3` 프로토콜은 엔드포인트를 리전별로
지정해야 하므로 확인해 두면 좋습니다.

```python
import boto3

s3 = boto3.client("s3")

for bucket in s3.list_buckets()["Buckets"]:
    location = s3.get_bucket_location(Bucket=bucket["Name"])["LocationConstraint"]
    # us-east-1은 역사적 이유로 None을 돌려준다
    region = location or "us-east-1"
    print(f"{bucket['Name']:<20} {region}")
```

```
dw-stage             ap-northeast-2
dw-archive           ap-northeast-2
etl-logs             us-east-1
```

### 버킷이 아주 많다면

`list_buckets` 는 오랫동안 페이지네이션이 없었지만, 최근 botocore는 `MaxBuckets` /
`ContinuationToken` 을 지원합니다. 버킷이 수백 개인 계정이라면 페이지네이터를
쓰되, 구버전에서도 동작하도록 예외를 받아두면 안전합니다.

```python
import boto3
from botocore.exceptions import OperationNotPageableError

s3 = boto3.client("s3")

def all_buckets() -> list:
    try:
        paginator = s3.get_paginator("list_buckets")
        return [b for page in paginator.paginate() for b in page.get("Buckets", [])]
    except OperationNotPageableError:
        # 구버전 botocore: 한 번에 전부 돌려준다
        return s3.list_buckets()["Buckets"]

print(len(all_buckets()))
```

### 특정 버킷에 접근 가능한지 확인

`list_buckets` 는 `s3:ListAllMyBuckets` 권한이 있어야 합니다. 이 권한 없이 특정
버킷만 쓸 수 있는 계정도 흔하므로, 목록이 비었다고 버킷이 없는 건 아닙니다.
개별 버킷 접근 여부는 `head_bucket` 으로 확인합니다.

```python
import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

def can_access(bucket: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "404":
            print(f"{bucket}: 버킷이 없습니다")
        elif code == "403":
            print(f"{bucket}: 권한이 없습니다")     # 존재는 하지만 접근 불가
        else:
            raise
        return False

can_access("dw-stage")
```

설정 파일의 버킷을 그대로 검사하려면:

```python
import sys

sys.path.insert(0, "src")
from s3_ops import read_s3_settings

can_access(read_s3_settings("conf/config.yaml")["bucket"])
```

## 2. 가장 기본 — 접두사 아래 파일 나열

`list_objects_v2` 는 한 번에 최대 1000개만 돌려줍니다. 파일이 그보다 많을 수 있으니
**항상 페이지네이터를 쓰세요.** 직접 호출하면 1000개에서 조용히 잘립니다.

```python
import boto3

s3 = boto3.client("s3")

paginator = s3.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket="dw-stage", Prefix="orders/")

for page in pages:
    # 결과가 하나도 없으면 Contents 키 자체가 없다
    for obj in page.get("Contents", []):
        print(f"{obj['Key']}\t{obj['Size']:,} bytes\t{obj['LastModified']}")
```

```
orders/2026-08-01/orders-00.csv.gz	 12,431,882 bytes	2026-08-03 04:12:31+00:00
orders/2026-08-01/orders-01.csv.gz	 12,402,117 bytes	2026-08-03 04:12:44+00:00
orders/2026-08-01/orders-02.csv.gz	  8,110,004 bytes	2026-08-03 04:12:51+00:00
```

## 3. 요약해서 보기 — 개수와 총 용량

파일이 세그먼트 수만큼 잘 나뉘었는지, 크기가 고른지 한눈에 확인합니다.

```python
import boto3

def summarize(bucket: str, prefix: str) -> None:
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")

    sizes = [
        obj["Size"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    ]
    if not sizes:
        print(f"s3://{bucket}/{prefix} 아래에 파일이 없습니다.")
        return

    total = sum(sizes)
    print(f"파일 {len(sizes)}개, 합계 {total / 1024 / 1024:.1f}MB")
    print(f"최소 {min(sizes) / 1024 / 1024:.1f}MB / "
          f"평균 {total / len(sizes) / 1024 / 1024:.1f}MB / "
          f"최대 {max(sizes) / 1024 / 1024:.1f}MB")

summarize("dw-stage", "orders/")
```

외부 테이블로 읽을 파일이라면 개수가 세그먼트 수보다 적을 때 병렬성을 다 못 씁니다.
쿼리를 쪼개 파일을 더 잘게 나누세요. 자세한 내용은
[S3 외부 테이블로 읽기](s3_external_table.md)에 있습니다.

## 4. 실행 단위로 묶어 보기 (Delimiter)

실행 단위마다 `{테이블명}/{날짜}/` 처럼 접두사를 나눠 올렸다면,
`Delimiter="/"` 를 줘서 파일 대신 그 "디렉터리" 목록만 받을 수 있습니다.

```python
import boto3

s3 = boto3.client("s3")
paginator = s3.get_paginator("list_objects_v2")

pages = paginator.paginate(
    Bucket="dw-stage",
    Prefix="orders/",
    Delimiter="/",          # 이 구분자 아래는 접어서 CommonPrefixes로 돌려준다
)

for page in pages:
    for entry in page.get("CommonPrefixes", []):
        print(entry["Prefix"])
```

```
orders/2026-08-01/
orders/2026-08-02/
orders/2026-08-03/
```

`Delimiter` 없이 부르면 하위 파일이 전부 평면적으로 나오고, 주면 한 단계만 봅니다.
실행이 몇 번 남아 있는지 훑을 때 편합니다. `bin/s3-ops ls` 도 같은 방식으로
디렉터리를 접어서 보여줍니다.

## 5. 오래 남은 찌꺼기 찾기

적재 후 지우는 것을 잊었거나 프로세스가 중간에 죽으면 파일이 남습니다. 하루 이상
지난 것만 골라냅니다.

```python
from datetime import datetime, timedelta, timezone

import boto3

def find_stale(bucket: str, prefix: str, older_than_hours: int = 24):
    s3 = boto3.client("s3")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    paginator = s3.get_paginator("list_objects_v2")

    return [
        obj
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
        if obj["LastModified"] < cutoff        # LastModified는 tz-aware datetime
    ]

stale = find_stale("dw-stage", "orders/")
for obj in sorted(stale, key=lambda o: o["LastModified"]):
    print(obj["LastModified"], obj["Key"], f"{obj['Size']:,}")
print(f"총 {len(stale)}개, {sum(o['Size'] for o in stale) / 1024**3:.2f}GB")
```

`LastModified` 는 UTC 기준 timezone-aware `datetime` 이므로, 비교 대상도
`timezone.utc` 를 붙여야 합니다. naive datetime과 비교하면 `TypeError` 가 납니다.

지운다면 `delete_objects` 로 한 번에 최대 1000개씩 묶어 보냅니다.

```python
def delete_all(bucket: str, keys: list[str]) -> None:
    s3 = boto3.client("s3")
    for start in range(0, len(keys), 1000):     # 요청당 1000개 제한
        batch = keys[start : start + 1000]
        response = s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        for error in response.get("Errors", []):
            print(f"삭제 실패: {error['Key']} ({error['Message']})")

# 실행 전에 반드시 목록을 눈으로 확인하세요
# delete_all("dw-stage", [o["Key"] for o in stale])
```

## 6. 올라간 파일 내용 확인

올라간 gzip 파일이 제대로 인코딩됐는지 앞부분만 열어봅니다. 전체를 받지 않고
`Range` 로 앞 몇 KB만 가져오면 큰 파일도 부담이 없습니다.

```python
import gzip

import boto3

s3 = boto3.client("s3")

def peek(bucket: str, key: str, lines: int = 5) -> None:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    with gzip.open(body, "rt", encoding="utf-8") as fp:
        for _, line in zip(range(lines), fp):
            # --delimiter $'\t' 로 뽑은 파일이라 탭 구분
            print(line.rstrip("\n").split("\t"))

peek("dw-stage", "orders/2026-08-01/orders.csv.gz")
```

```
['1', '김철수', '\\N']
['2', '이영희', '10.50']
```

`get_object` 의 `Body` 는 스트리밍 객체라서 `gzip.open` 에 그대로 넘길 수 있습니다.
전체를 메모리에 올리지 않습니다.

## 7. 파일 하나의 메타데이터만 확인

존재 여부와 크기만 알면 될 때는 `head_object` 가 가볍습니다.

```python
import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

def exists(bucket: str, key: str) -> bool:
    try:
        meta = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise            # 권한 오류 등은 그대로 올린다
    print(f"{meta['ContentLength']:,} bytes, {meta['LastModified']}")
    return True
```

없는 키에 대해 `404` 를 그냥 삼키면 권한 문제(`403`)까지 "없음"으로 처리되니,
위처럼 에러 코드를 구분해서 다뤄야 합니다.

## 8. 설정 파일을 그대로 재사용하기

`conf/config.yaml` 에 이미 버킷과 자격증명이 있으니, 목록 확인 스크립트에서도 같은 설정을
쓰면 됩니다. `bin/s3-ops` 가 기본으로 읽는 파일이고, `src/s3_ops.py` 의
`read_s3_settings` 가 s3 섹션에서 접속에 필요한 값만 읽어 `${ENV_VAR}` 참조까지
치환해 돌려줍니다.

```python
import sys

import boto3

sys.path.insert(0, "src")
from s3_ops import read_s3_settings

settings = read_s3_settings("conf/config.yaml")
session = boto3.session.Session(
    aws_access_key_id=settings["access_key_id"],
    aws_secret_access_key=settings["secret_access_key"],
    region_name=settings["region"],
)
client = session.client("s3", endpoint_url=settings["client_endpoint_url"] or None)

paginator = client.get_paginator("list_objects_v2")
for page in paginator.paginate(Bucket=settings["bucket"], Prefix="orders/"):
    for obj in page.get("Contents", []):
        print(obj["Key"], f"{obj['Size']:,}")
```

같은 일을 명령행에서 하려면:

```bash
bin/s3-ops ls s3://dw-stage/orders/      # 설정은 자동으로 읽힙니다
```

## S3 호환 스토리지(MinIO 등)

엔드포인트만 바꿔주면 나머지는 동일합니다.

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url="http://minio.example.com:9000",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
)
```

설정 파일에서는 `s3.client_endpoint_url` 이 이 값에 대응하고, `bin/s3-ops` 는
`--endpoint` 로도 받습니다.

**이건 boto3가 올릴 때 쓰는 주소입니다.** Greenplum이 읽을 때 쓰는 주소는 외부 테이블
LOCATION이나 `s3.conf` 에 따로 적으며, 두 값이 다를 수 있습니다(예: 업로드는 내부망
주소로, 세그먼트는 다른 경로로 접근하는 경우).

## 자주 걸리는 것들

| 증상 | 원인 |
| --- | --- |
| 파일이 1000개에서 끊긴다 | `list_objects_v2` 직접 호출. 페이지네이터를 쓰세요. |
| `list_buckets` 가 `AccessDenied` | `s3:ListAllMyBuckets` 권한이 없습니다. 버킷을 이미 안다면 `head_bucket` 으로 접근만 확인하세요. |
| 버킷 목록이 비어 보인다 | 권한 범위가 특정 버킷으로 한정된 계정입니다. 버킷이 없다는 뜻이 아닙니다. |
| `get_bucket_location` 이 `None` | `us-east-1` 은 역사적 이유로 `LocationConstraint` 가 `None` 입니다. |
| `KeyError: 'Contents'` | 결과가 비면 `Contents` 키가 아예 없습니다. `page.get("Contents", [])` 로 받으세요. |
| `TypeError: can't compare offset-naive and offset-aware datetimes` | `LastModified` 는 tz-aware입니다. 비교 대상에 `timezone.utc` 를 붙이세요. |
| 하위 디렉터리가 안 보인다 | S3에 디렉터리는 없습니다. `Delimiter="/"` 로 `CommonPrefixes` 를 받으세요. |
| 목록은 되는데 Greenplum이 못 읽는다 | boto3 자격증명과 세그먼트의 `s3.conf` 는 별개입니다. `gpcheckcloud` 로 확인하세요. |
