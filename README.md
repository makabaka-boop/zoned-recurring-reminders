# recurring_reminders

Python + SQLite 的循环事件提醒服务。每日/每周循环、真实时区语义、单日例外、
规则版本化证据、幂等提醒生成器。零第三方依赖（仅标准库），Python 3.9+。

## 核心语义

**本地时间 → UTC 永远经由 tz 数据库解析**（`zoneinfo`），不使用固定 UTC 偏移：

- **不存在的本地时间**（春季拨快产生的时间缺口）：按系列的 `on_gap` 策略处理 —
  - `skip`：该实例跳过，记录跳过原因；
  - `shift`：移到下一个有效本地时刻（如 02:30 → 03:30），并记录原因。
- **重复出现的本地时间**（秋季拨回）：按系列的 `on_ambiguous` 策略取
  `first`（UTC 较早的那次）或 `second`（较晚的那次）。
- 缺口/重复的判定通过对 UTC 往返转换完成，不依赖任何 fold/偏移约定。

**规则版本化**：修改系列（`modify_series`）只插入一条新的 `series_versions`
行，从指定的 `effective_from`（本地日期，含当日）起生效；之前的日期自动沿用
旧版本。实例在以下时刻被**确认**（confirmed）：显式调用 confirm API，或其提醒
被生成。确认即冻结证据——版本号、规则本地时间、例外修正后时间、缺口平移后
时间、UTC 时刻、状态、跳过原因全部落库且不再改写，之后改系列或加例外都不影响。

**单日例外**（`cancel` / `move`+`override_time`）：只作用于尚未确认的实例，
在计算时生效；已确认实例保留原证据。

**幂等提醒生成器**：`tick()` 用注入的时钟计算 `horizon = now + 15分钟`，为所有
`utc_start <= horizon` 且尚无提醒的已排程实例写入提醒。提醒表以
`(series_id, local_date)` 为唯一键，`INSERT OR IGNORE` + 单事务，因此重启、
重复推进时钟、例外与系列修改交错都不会漏记或重记；时钟向前跳跃时，过期的
提醒会补发（迟到但不丢）。每次 tick 写入 `generator_runs` 审计行。

## 布局

```
recurring_reminders/
  clock.py       可控时钟（SystemClock / ManualClock）
  tzresolve.py   本地时间 → UTC 解析（缺口/重复策略）
  recurrence.py  每日/每周日期展开（interval、byweekday、until）
  store.py       SQLite schema
  service.py     业务逻辑：系列 CRUD、版本化、例外、确认、生成器
  api.py         标准库 HTTP JSON API
  __main__.py    python -m recurring_reminders --db x.db --port 8080
tests/
  pinned_tz.py   固定时区数据：自生成 TZif v2 文件（不依赖宿主 tzdata）
  test_*.py      unittest 测试
```

## 运行测试

```bash
cd /workspace
python3 -m unittest discover -v
```

测试用时区数据由 `tests/pinned_tz.py` 现场生成 TZif 文件并经
`ZoneInfo.from_file` 加载，与宿主 tzdata 版本无关：

- `Test/USlike`：3 月第二个周日 02:00→03:00（-5→-4），11 月第一个周日
  02:00→01:00（-4→-5），2025–2030 年转换时刻写死 + POSIX footer；
- `Test/East`：固定 UTC+10，用于跨日提醒测试。

覆盖：时间缺口（skip/shift）、重复时刻（first/second）、跨日提醒
（前一日 UTC 到期）、15 分钟边界、重启后幂等、时钟跳跃补发、
修改/例外交错、确认实例证据保留。

## HTTP API

```bash
python3 -m recurring_reminders --db reminders.db --port 8080 \
    [--manual-clock 2026-03-07T00:00:00+00:00]
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/series` | 创建系列（name, tz, freq, time_of_day, start_date, interval?, byweekday?, until?, on_gap?, on_ambiguous?） |
| GET | `/series/{id}` | 系列 + 全部规则版本 + 例外（证据） |
| POST | `/series/{id}/versions` | 修改系列：`{"effective_from": "YYYY-MM-DD", ...字段}` |
| POST | `/series/{id}/exceptions` | 单日例外：`{"date", "action": "cancel"\|"move", "override_time"?}` |
| GET | `/series/{id}/occurrences?from=&to=` | 实例列表：本地时间、UTC、规则版本、跳过原因 |
| POST | `/series/{id}/confirm` | 冻结某日期实例证据：`{"date"}` |
| POST | `/tick` | 运行生成器；`{"now": ISO}` 先推进可控时钟 |
| GET | `/reminders?series_id=` | 提醒列表 |
| GET | `/runs` | 生成器审计日志 |

实例响应示例（同时给出本地时间、UTC 时刻、规则版本、跳过原因）：

```json
{
  "series_id": 1, "local_date": "2026-03-08", "tz": "Test/USlike",
  "version": 1,
  "intended_local": "2026-03-08T02:30",
  "effective_local": "2026-03-08T02:30",
  "actual_local": "2026-03-08T03:30",
  "utc": "2026-03-08T07:30:00+00:00",
  "status": "scheduled", "kind": "gap-shifted",
  "skip_reason": "local time 2026-03-08 02:30 does not exist in Test/USlike (clocks jump forward); moved to next valid local time 2026-03-08 03:30 per series policy on_gap=shift",
  "exception": null, "confirmed": true
}
```

## 设计要点

- **实例身份** = `(series_id, local_date)`；确认表与提醒表都以此唯一。
- **确认即证据**：`occurrences` 只存已确认实例；未确认实例按需实时计算，
  因此例外/修改在确认前交错总能生效，确认后永远冻结。
- **版本选择**：对本地日期 D 取 `effective_from <= D` 的最大版本
  （按 `(effective_from, version)` 排序），修改天然只影响生效日之后的实例。
- **不漏记**：生成器下界从系列起点扫到 `horizon` 本地日 +1 天
  （任何真实时区 |offset| < 24h），过期未记的提醒一律补发；
  **不重记**：唯一约束 + `INSERT OR IGNORE`，重启/重推/交错均安全。
