# sql/

`bin/query-to-csv` 가 `--query-file` 로 읽는 `.sql` 파일을 모아두는 곳입니다.
파일 이름만 주면 여기서 찾습니다.

```bash
bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 -o orders.csv
```

다른 디렉터리를 쓰려면 `conf/config.yaml` 의 `sql.dir` 을 바꾸거나 `--sql-dir` 로
그때만 지정합니다. 설정에 적은 상대 경로는 설정 파일이 있는 `conf/` 기준입니다.

```yaml
sql:
  dir: ../sql
```

## Jinja 템플릿

`.sql` 파일은 [Jinja](https://jinja.palletsprojects.com/) 템플릿입니다. `{{ 변수 }}`
자리에 `--var KEY=VALUE` 로 준 값이 들어갑니다.

```sql
SELECT * FROM sales.orders WHERE order_dt = '{{ dt }}'
```

```bash
bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 -o orders.csv
```

조건문과 반복문도 씁니다.

```sql
SELECT *
  FROM sales.orders
 WHERE order_dt = '{{ dt }}'
{% if status %}
   AND status = '{{ status }}'
{% endif %}
```

`--var status=...` 를 주지 않으면 그 줄이 통째로 빠집니다. 다만 **`{{ }}` 로 참조한
변수를 주지 않으면 오류입니다.** Jinja 기본 동작은 빈 문자열로 조용히 채우는
것인데, `WHERE order_dt = ''` 같은 문장이 만들어져 0건을 돌려주는 편이 훨씬
위험해서 그렇게 두지 않았습니다.

```
sql/daily_orders.sql 의 템플릿 변수를 채우지 못했습니다: 'dt' is undefined
  지금 준 변수: (없음)
  --var KEY=VALUE 로 지정하세요.
```

값이 없을 때 기본값을 쓰고 싶다면 템플릿에 적습니다.

```sql
WHERE order_dt = '{{ dt | default("2026-08-01") }}'
```

## 따옴표는 템플릿이 책임집니다

**값은 SQL에 그대로 들어갑니다.** 위 예제들이 `'{{ dt }}'` 처럼 따옴표를 템플릿
쪽에 두는 이유입니다. 값에 따옴표가 섞이면 쿼리가 깨지거나 의도치 않은 SQL이
만들어집니다.

직접 치는 값이라면 문제될 게 없지만, 다른 시스템에서 받은 값을 그대로 넘긴다면
넘기기 전에 검증하세요. 날짜나 숫자처럼 형식이 정해진 값은 그 형식인지 확인하는
것만으로 충분합니다.

## 확인하는 법

`--debug` 를 주면 **템플릿을 채운 뒤 실제로 서버에 보내는 SQL** 을 출력합니다.
변수가 의도대로 들어갔는지 여기서 확인하세요.

```bash
bin/query-to-csv -f daily_orders.sql --var dt=2026-08-01 -o orders.csv --debug
```

```
--- 실행할 SQL ---
SELECT * FROM sales.orders WHERE order_dt = '2026-08-01'
------------------
```

## 파일 목록

`--query-file` 에 없는 이름을 주면 여기 있는 `.sql` 파일을 나열해 줍니다.
