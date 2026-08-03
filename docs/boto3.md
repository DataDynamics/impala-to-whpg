# boto3로 S3 파일 목록 보기

`load_method: s3` 로 적재할 때 만들어지는 스테이징 파일을 확인하거나, 실패한 작업이
남긴 찌꺼기를 찾을 때 쓰는 예제입니다. `cleanup: false` 로 두고 파일을 살펴보면
디버깅이 훨씬 수월합니다.

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

## 1. 가장 기본 — 접두사 아래 파일 나열

`list_objects_v2` 는 한 번에 최대 1000개만 돌려줍니다. 파일이 그보다 많을 수 있으니
**항상 페이지네이터를 쓰세요.** 직접 호출하면 1000개에서 조용히 잘립니다.

```python
import boto3

s3 = boto3.client("s3")

paginator = s3.get_paginator("list_objects_v2")
pages = paginator.paginate(Bucket="dw-stage", Prefix="impala-to-greenplum/")

for page in pages:
    # 결과가 하나도 없으면 Contents 키 자체가 없다
    for obj in page.get("Contents", []):
        print(f"{obj['Key']}\t{obj['Size']:,} bytes\t{obj['LastModified']}")
```

```
impala-to-greenplum/orders-9f2c1a7b/part-00000.tsv.gz	 12,431,882 bytes	2026-08-03 04:12:31+00:00
impala-to-greenplum/orders-9f2c1a7b/part-00001.tsv.gz	 12,402,117 bytes	2026-08-03 04:12:44+00:00
impala-to-greenplum/orders-9f2c1a7b/part-00002.tsv.gz	  8,110,004 bytes	2026-08-03 04:12:51+00:00
```

## 2. 요약해서 보기 — 개수와 총 용량

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

summarize("dw-stage", "impala-to-greenplum/")
```

파일 개수가 세그먼트 수보다 적으면 병렬성을 다 못 씁니다. `s3.file_size_mb` 를
줄이세요. 자세한 내용은 [S3 외부 테이블 적재 설정](s3_external_table.md)에 있습니다.

## 3. 실행 단위로 묶어 보기 (Delimiter)

이 프로젝트는 실행마다 `{prefix}/{테이블명}-{난수}/` 아래에 파일을 올립니다.
`Delimiter="/"` 를 주면 파일 대신 그 "디렉터리" 목록만 받을 수 있습니다.

```python
import boto3

s3 = boto3.client("s3")
paginator = s3.get_paginator("list_objects_v2")

pages = paginator.paginate(
    Bucket="dw-stage",
    Prefix="impala-to-greenplum/",
    Delimiter="/",          # 이 구분자 아래는 접어서 CommonPrefixes로 돌려준다
)

for page in pages:
    for entry in page.get("CommonPrefixes", []):
        print(entry["Prefix"])
```

```
impala-to-greenplum/customers-3b7e11d2/
impala-to-greenplum/orders-9f2c1a7b/
impala-to-greenplum/orders-c04af881/
```

`Delimiter` 없이 부르면 하위 파일이 전부 평면적으로 나오고, 주면 한 단계만 봅니다.
실행이 몇 번 남아 있는지 훑을 때 편합니다.

## 4. 오래 남은 찌꺼기 찾기

`cleanup` 을 꺼둔 채 돌렸거나 프로세스가 강제 종료되면 파일이 남을 수 있습니다.
하루 이상 지난 것만 골라냅니다.

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

stale = find_stale("dw-stage", "impala-to-greenplum/")
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

## 5. 스테이징 파일 내용 확인

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
            # COPY TEXT 포맷이라 탭 구분, NULL은 \N
            print(line.rstrip("\n").split("\t"))

peek("dw-stage", "impala-to-greenplum/orders-9f2c1a7b/part-00000.tsv.gz")
```

```
['1', '김철수', '\\N']
['2', '이영희', '10.50']
```

`get_object` 의 `Body` 는 스트리밍 객체라서 `gzip.open` 에 그대로 넘길 수 있습니다.
전체를 메모리에 올리지 않습니다.

## 6. 파일 하나의 메타데이터만 확인

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

## 7. 이 프로젝트 설정을 그대로 재사용하기

`config.yaml` 에 이미 버킷과 자격증명이 있으니, 목록 확인 스크립트에서도 같은 설정을
쓰면 됩니다.

```python
from impala_to_greenplum import load_config
from impala_to_greenplum.s3_stage import S3Stager

config = load_config("config.yaml")
stager = S3Stager(config.s3)

paginator = stager.client.get_paginator("list_objects_v2")
prefix = config.s3.prefix.strip("/") + "/"

for page in paginator.paginate(Bucket=config.s3.bucket, Prefix=prefix):
    for obj in page.get("Contents", []):
        print(obj["Key"], f"{obj['Size']:,}")
```

`S3Stager.client` 는 설정의 자격증명·리전·`client_endpoint_url`(MinIO 등)을 반영해
만들어진 boto3 클라이언트라, 별도 설정 없이 바로 쓸 수 있습니다.

바로 실행할 수 있는 스크립트는 `examples/list_staged_files.py` 에 있습니다.

```bash
python examples/list_staged_files.py --config config.yaml
python examples/list_staged_files.py --config config.yaml --group   # 실행 단위로 묶어 보기
python examples/list_staged_files.py --config config.yaml --stale 24
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

설정 파일에서는 `s3.client_endpoint_url` 이 이 값에 대응합니다. 이건 boto3가
업로드할 때 쓰는 주소이고, Greenplum이 읽을 때 쓰는 주소는 `s3.endpoint` 로 따로
지정한다는 점에 주의하세요. 두 값이 다를 수 있습니다(예: 파이썬은 내부망 주소로,
세그먼트는 다른 경로로 접근하는 경우).

## 자주 걸리는 것들

| 증상 | 원인 |
| --- | --- |
| 파일이 1000개에서 끊긴다 | `list_objects_v2` 직접 호출. 페이지네이터를 쓰세요. |
| `KeyError: 'Contents'` | 결과가 비면 `Contents` 키가 아예 없습니다. `page.get("Contents", [])` 로 받으세요. |
| `TypeError: can't compare offset-naive and offset-aware datetimes` | `LastModified` 는 tz-aware입니다. 비교 대상에 `timezone.utc` 를 붙이세요. |
| 하위 디렉터리가 안 보인다 | S3에 디렉터리는 없습니다. `Delimiter="/"` 로 `CommonPrefixes` 를 받으세요. |
| 목록은 되는데 Greenplum이 못 읽는다 | boto3 자격증명과 세그먼트의 `s3.conf` 는 별개입니다. `gpcheckcloud` 로 확인하세요. |
