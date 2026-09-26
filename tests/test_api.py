"""API 层测试：正常展开与 422 校验。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = {
    "timezone": "America/New_York",
    "start_date": "2026-03-08",
    "end_date": "2026-03-08",
    "weekdays": [6],
    "local_time": "02:30",
    "gap_strategy": "shift_forward",
    "fold_strategy": "both",
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_expand_gap_shift_forward():
    resp = client.post("/expand", json=BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["skipped_count"] == 0
    inst = body["instances"][0]
    assert inst["local"] == "2026-03-08T03:30:00-04:00"
    assert inst["offset"] == "-04:00"
    assert inst["utc"] == "2026-03-08T07:30:00Z"
    assert inst["reason"] == "gap_shifted_forward"


def test_expand_gap_skip():
    resp = client.post("/expand", json={**BASE, "gap_strategy": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped_dates"] == ["2026-03-08"]


def test_expand_fold_both():
    payload = {
        **BASE,
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "local_time": "01:30",
    }
    resp = client.post("/expand", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    first, second = body["instances"]
    assert first["reason"] == "fold_first"
    assert first["offset"] == "-04:00"
    assert first["utc"] == "2026-11-01T05:30:00Z"
    assert second["reason"] == "fold_second"
    assert second["offset"] == "-05:00"
    assert second["utc"] == "2026-11-01T06:30:00Z"
    # 本地时钟面相同，但为两个不同的 UTC 实例
    assert first["local"] == "2026-11-01T01:30:00-04:00"
    assert second["local"] == "2026-11-01T01:30:00-05:00"


def test_invalid_timezone_422():
    resp = client.post("/expand", json={**BASE, "timezone": "Not/AZone"})
    assert resp.status_code == 422


def test_reversed_range_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-04-01", "end_date": "2026-03-01"},
    )
    assert resp.status_code == 422


def test_range_over_366_days_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-01-01", "end_date": "2027-01-03"},
    )
    assert resp.status_code == 422


def test_range_exactly_366_days_ok():
    resp = client.post(
        "/expand",
        json={
            **BASE,
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",  # 相差 366 天
            "local_time": "09:00",
        },
    )
    assert resp.status_code == 200


def test_invalid_weekday_422():
    resp = client.post("/expand", json={**BASE, "weekdays": [7]})
    assert resp.status_code == 422


def test_empty_weekdays_422():
    resp = client.post("/expand", json={**BASE, "weekdays": []})
    assert resp.status_code == 422


# --- POST /overlaps ---------------------------------------------------------

# A 侧：柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，持续 60 分钟
OVERLAP_A = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
}
# B 侧：纽约同日 03:30 EST = 08:30 UTC，持续 60 分钟
OVERLAP_B = {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60,
}


def test_overlaps_basic():
    resp = client.post("/overlaps", json={"a": OVERLAP_A, "b": OVERLAP_B})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["a_start_utc"] == "2026-03-02T08:00:00Z"
    assert ov["b_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-02T09:00:00Z"
    assert ov["minutes"] == 30


def test_overlaps_boundary_touch_not_conflict():
    # B 改为 04:00 EST = 09:00 UTC，与 A 的终点 09:00 UTC 恰好相接
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "local_time": "04:00"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_sorted_multiple():
    # 两侧都扩到周一、周二；A 09:00+120min，B 04:30 EST+60min = 09:30 UTC
    a = {
        **OVERLAP_A,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "duration_minutes": 120,
    }
    b = {
        **OVERLAP_B,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "local_time": "04:30",
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    starts = [ov["overlap_start_utc"] for ov in body["overlaps"]]
    assert starts == sorted(starts)
    assert starts == ["2026-03-02T09:30:00Z", "2026-03-03T09:30:00Z"]
    assert all(ov["overlap_end_utc"].endswith("T10:00:00Z") for ov in body["overlaps"])
    assert all(ov["minutes"] == 30 for ov in body["overlaps"])


def test_overlaps_fold_both_counts_first_occurrence():
    # 纽约 2026-11-01 01:30 出现两次：05:30 UTC / 06:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "05:45",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "01:30",
        "fold_strategy": "both",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    # 仅第一次出现 [05:30, 06:30) 与 A [05:45, 06:15) 重叠
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-11-01T05:30:00Z"
    assert ov["overlap_start_utc"] == "2026-11-01T05:45:00Z"
    assert ov["overlap_end_utc"] == "2026-11-01T06:15:00Z"
    assert ov["minutes"] == 30

    # 只取第二次出现时不再重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "fold_strategy": "second"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_gap_strategies():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "07:30",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "02:30",
        "gap_strategy": "shift_forward",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-08T08:00:00Z"
    assert ov["minutes"] == 30

    # skip 策略下该次被跳过，无重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "gap_strategy": "skip"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_duration_zero_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "duration_minutes": 0}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422


def test_overlaps_duration_over_1440_422():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1441}},
    )
    assert resp.status_code == 422


def test_overlaps_duration_1440_ok():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1440}},
    )
    assert resp.status_code == 200


def test_overlaps_side_range_over_366_days_422():
    resp = client.post(
        "/overlaps",
        json={
            "a": OVERLAP_A,
            "b": {**OVERLAP_B, "start_date": "2026-01-01", "end_date": "2027-01-03"},
        },
    )
    assert resp.status_code == 422


def test_overlaps_invalid_timezone_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "timezone": "Not/AZone"}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422
