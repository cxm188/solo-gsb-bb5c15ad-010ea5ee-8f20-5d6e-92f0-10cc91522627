"""核心展开逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scheduler import (
    Instance,
    classify,
    daily_occupancy,
    expand_schedule,
    find_overlaps,
    merge_intervals,
)

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def test_classify_normal():
    kind, payload = classify(datetime(2026, 3, 7, 2, 30), NY)
    assert kind == "normal"
    assert payload[0] == datetime(2026, 3, 7, 7, 30, tzinfo=timezone.utc)


def test_classify_gap():
    # 2026-03-08 02:30 在纽约不存在（02:00 -> 03:00）
    kind, gap = classify(datetime(2026, 3, 8, 2, 30), NY)
    assert kind == "gap"
    assert gap == timedelta(hours=1)


def test_classify_fold():
    # 2026-11-01 01:30 在纽约出现两次
    kind, payload = classify(datetime(2026, 11, 1, 1, 30), NY)
    assert kind == "fold"
    first, second = payload
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)  # EDT
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)  # EST


def test_gap_skip():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={0, 1, 2, 3, 4, 5, 6},
        local_time=time(2, 30),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert instances == []
    assert skipped == [date(2026, 3, 8)]


def test_gap_shift_forward():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 EDT -> 07:30 UTC
    assert inst.utc == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert inst.utc.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)


def test_fold_strategies():
    base = dict(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
    )
    first, _ = expand_schedule(fold_strategy="first", **base)
    assert [i.reason for i in first] == ["fold_first"]
    assert first[0].utc == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    second, _ = expand_schedule(fold_strategy="second", **base)
    assert [i.reason for i in second] == ["fold_second"]
    assert second[0].utc == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    both, _ = expand_schedule(fold_strategy="both", **base)
    assert [i.reason for i in both] == ["fold_first", "fold_second"]
    assert both[0].utc < both[1].utc


def test_weekday_filter_and_utc_sort():
    # 2026-03-01 是周日；每周一、三 09:00，柏林时间
    instances, skipped = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 15),
        weekdays={0, 2},
        local_time=time(9, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 4  # 3/2, 3/4, 3/9, 3/11
    utcs = [i.utc for i in instances]
    assert utcs == sorted(utcs)
    assert len(set(utcs)) == len(utcs)  # 已去重
    assert all(i.reason == "normal" for i in instances)


def test_berlin_gap_shift():
    # 柏林 2026-03-29 02:30 不存在（02:00 -> 03:00）
    instances, skipped = expand_instances = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 29),
        end_date=date(2026, 3, 29),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="both",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 CEST -> 01:30 UTC
    assert inst.utc == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


def _inst(hh: int, mm: int = 0) -> Instance:
    return Instance(datetime(2026, 1, 1, hh, mm, tzinfo=timezone.utc), "normal")


def test_find_overlaps_basic():
    # A [08:00, 09:00)，B [08:30, 09:30) -> 重叠 [08:30, 09:00)
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=60,
    )
    assert ov.a_start == datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert ov.b_start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_boundary_touch_not_conflict():
    # 半开区间：A [08:00, 09:00) 与 B [09:00, 10:00) 边界相接，不算冲突
    assert (
        find_overlaps(
            a_instances=[_inst(8)],
            a_duration_minutes=60,
            b_instances=[_inst(9)],
            b_duration_minutes=60,
        )
        == []
    )


def test_find_overlaps_containment():
    # B 完全包含于 A
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=120,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=30,
    )
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_multiple_active_per_side():
    # A 侧两个并行区间都与同一 B 区间重叠，结果按重叠起点排序
    overlaps = find_overlaps(
        a_instances=[_inst(8), _inst(8, 30)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 15)],
        b_duration_minutes=60,
    )
    assert len(overlaps) == 2
    assert [o.start for o in overlaps] == [
        datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    ]
    assert all(o.minutes == 45 for o in overlaps)


def test_find_overlaps_empty_side():
    assert (
        find_overlaps(
            a_instances=[],
            a_duration_minutes=60,
            b_instances=[_inst(8)],
            b_duration_minutes=60,
        )
        == []
    )


# --- merge_intervals --------------------------------------------------------

def test_merge_intervals_overlapping_and_touching():
    intervals = [
        (datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
         datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)),
        (datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc),  # 相交
         datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)),
        (datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),  # 相接也合并
         datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)),
        (datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),  # 有间隙，独立
         datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)),
    ]
    assert merge_intervals(intervals) == [
        (datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
         datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)),
        (datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc),
         datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)),
    ]


def test_merge_intervals_empty():
    assert merge_intervals([]) == []


# --- daily_occupancy --------------------------------------------------------

def _occ(insts, minutes, tz):
    return [(d, float(m)) for d, m in daily_occupancy(
        instances=insts, duration_minutes=minutes, report_tz=tz
    )]


def test_daily_occupancy_cross_midnight_utc():
    # [23:00, 24:00) 与 [23:30, 00:30) 合并为 [23:00, 00:30)
    insts = [_inst(23), _inst(23, 30)]
    assert _occ(insts, 60, timezone.utc) == [
        (date(2026, 1, 1), 60.0),
        (date(2026, 1, 2), 30.0),
    ]


def test_daily_occupancy_empty():
    assert daily_occupancy(
        instances=[], duration_minutes=60, report_tz=timezone.utc
    ) == []


def test_daily_occupancy_gap_day_is_23_hours():
    # 纽约春跳日：01:00 EST(06:00 UTC) 起 1440 分钟，至次日 06:00 UTC；
    # 本地日界 05:00 UTC -> 次日 04:00 UTC（短日）
    inst = Instance(datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc), "normal")
    assert _occ([inst], 1440, NY) == [
        (date(2026, 3, 8), 1320.0),  # 01:00 至本地午夜（短日 23h 少 1h）
        (date(2026, 3, 9), 120.0),
    ]


def test_daily_occupancy_fold_day_is_25_hours():
    # 纽约秋回日：00:00 EDT(04:00 UTC) 起 1560 分钟，至次日 06:00 UTC
    # （本地 01:00 EST）；长日本地午夜在 05:00 UTC
    inst = Instance(datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc), "normal")
    assert _occ([inst], 1560, NY) == [
        (date(2026, 11, 1), 1500.0),  # 整个 25 小时本地日占满
        (date(2026, 11, 2), 60.0),
    ]


def test_daily_occupancy_fold_both_overlap_counted_once():
    # 01:30 的两次出现，各 60 分钟：[05:30,06:30) 与 [06:30,07:30)
    # 相接合并为 120 分钟；本地 01:00–03:30 共 150 分钟内占用 120
    insts, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
        fold_strategy="both",
    )
    assert _occ(insts, 60, NY) == [(date(2026, 11, 1), 120.0)]


def test_daily_occupancy_two_25h_days_merge_continues():
    # 秋回前后各一次 1500 分钟：00:00 EDT [04:00 UTC, +25h=05:00 UTC 次日)
    # 与次日 00:00 EST [05:00 UTC, 06:00 UTC) 相接，合并为连续 50 小时
    insts, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        weekdays={6, 0},
        local_time=time(0, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert _occ(insts, 1500, NY) == [
        (date(2026, 11, 1), 1500.0),  # 25 小时本地日占满
        (date(2026, 11, 2), 1440.0),  # 普通 24 小时日占满
        (date(2026, 11, 3), 60.0),
    ]
