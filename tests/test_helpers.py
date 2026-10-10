from datetime import date, datetime, timezone

from app.main import (
    late_minutes, work_hours, overtime_minutes, working_days,
    department_trend_pipeline, leaderboard_pipeline,
)


def test_late_grace_is_strictly_more_than_ten_minutes():
    assert late_minutes(datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc),
                        "09:30", "2026-07-06") == 0
    assert late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc),
                        "09:30", "2026-07-06") == 10


def test_work_hours_are_rounded_half_up():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert work_hours(start, start.replace(second=30)) == 0.01


def test_overnight_overtime_uses_next_day_shift_end():
    punch_out = datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc)  # 06:40 IST
    assert overtime_minutes(punch_out, "06:00", "2026-07-06", "22:00") == 40


def test_weekday_count():
    assert working_days(date(2026, 7, 1), date(2026, 7, 31)) == 23


def test_trend_is_database_pipeline_and_has_window_stage():
    pipeline = department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
    assert any("$setWindowFields" in stage for stage in pipeline)
    assert any("$lookup" in stage for stage in pipeline)
    assert len(pipeline) > 0


def test_leaderboard_ranks_before_limit_filter():
    pipeline = leaderboard_pipeline("2026-07", None, 2)
    rank_index = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
    limit_index = next(i for i, stage in enumerate(pipeline) if stage.get("$match", {}).get("rank"))
    assert rank_index < limit_index


def test_overnight_punch_in_attendance_date():
    from app.main import attendance_date
    # Overnight shift 22:00 to 06:00
    # Punch in at 01:10 IST on July 7th belongs to shift starting July 6th
    punch_in_early_morning = datetime(2026, 7, 6, 19, 40, tzinfo=timezone.utc)  # 01:10 IST on 2026-07-07
    assert attendance_date(punch_in_early_morning, "22:00", "06:00") == "2026-07-06"
    
    # Punch in before midnight on July 6th at 21:55 IST
    punch_in_night = datetime(2026, 7, 6, 16, 25, tzinfo=timezone.utc)  # 21:55 IST on 2026-07-06
    assert attendance_date(punch_in_night, "22:00", "06:00") == "2026-07-06"

    # Regular daytime shift
    daytime_punch = datetime(2026, 7, 7, 3, 58, tzinfo=timezone.utc)  # 09:28 IST on 2026-07-07
    assert attendance_date(daytime_punch, "09:30", "18:30") == "2026-07-07"


def test_r1_instants_truncated_to_whole_seconds():
    from app.main import from_epoch_ms, epoch_ms
    # 1783312500900 ms has 900 ms
    dt = from_epoch_ms(1783312500900)
    assert dt.microsecond == 0
    # converting back drops fractional seconds
    assert epoch_ms(dt) == 1783312500000

