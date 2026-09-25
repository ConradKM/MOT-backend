"""app/queueing/eta.py - the walk-in ETA simulation, against fabricated
appointments and queue states (no database)."""

from datetime import UTC, datetime, timedelta

from app.queueing.eta import (
    OVERRUN_GRACE,
    Busy,
    QueuedJob,
    estimate_queue,
    ongoing,
    wait_minutes,
)

DAY = datetime(2026, 3, 2, tzinfo=UTC)  # a Monday


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


def jobs(*minutes):
    return [QueuedJob(f"job{i}", m) for i, m in enumerate(minutes)]


def starts(estimates):
    return [e.start for e in estimates]


OPENS = DAY.replace(hour=9)
CLOSES = DAY.replace(hour=17)


def run(*, now, queue, busy=(), capacity=1, opens=OPENS, closes=CLOSES):
    return estimate_queue(
        now=now, capacity=capacity, opens_at=opens, closes_at=closes, busy=busy, queue=queue
    )


# --- basic queueing ------------------------------------------------------


def test_empty_garage_single_bay_is_back_to_back():
    result = run(now=at(10), queue=jobs(30, 30, 30))
    assert starts(result) == [at(10), at(10, 30), at(11)]
    assert [e.position for e in result] == [1, 2, 3]
    assert all(e.fits_today for e in result)


def test_parallel_bays_serve_concurrently_not_one_at_a_time():
    result = run(now=at(10), queue=jobs(30, 30, 30, 30, 30), capacity=2)
    assert starts(result) == [at(10), at(10), at(10, 30), at(10, 30), at(11)]


def test_uneven_durations_across_bays_take_the_first_free_bay():
    # Bay A: 60-min job; bay B: 20-min job. Third person gets bay B at 10:20.
    result = run(now=at(10), queue=jobs(60, 20, 30), capacity=2)
    assert starts(result) == [at(10), at(10), at(10, 20)]


def test_before_opening_the_queue_starts_at_opening():
    result = run(now=at(8, 15), queue=jobs(30, 30))
    assert starts(result) == [at(9), at(9, 30)]


def test_empty_queue_returns_nothing():
    assert run(now=at(10), queue=[]) == []


# --- appointments competing for the same bays ----------------------------


def test_booking_later_today_blocks_a_job_that_would_overlap_it():
    # 13:30 now, one bay, 14:00-15:00 booked. A 45-minute walk-in can't finish
    # before 14:00, so it waits for the booking to end.
    busy = [Busy(at(14), at(15))]
    assert starts(run(now=at(13, 30), queue=jobs(45), busy=busy)) == [at(15)]


def test_job_ending_exactly_when_a_booking_starts_fits_before_it():
    busy = [Busy(at(14), at(15))]
    assert starts(run(now=at(13, 30), queue=jobs(30), busy=busy)) == [at(13, 30)]


def test_fully_booked_afternoon_pushes_the_whole_queue_back():
    # Both bays booked 13:00-17:00; the garage closes 17:30.
    busy = [Busy(at(13), at(17)), Busy(at(13), at(17))]
    result = run(now=at(13), queue=jobs(30, 30, 30), busy=busy, capacity=2, closes=at(17, 30))
    assert starts(result) == [at(17), at(17), None]
    assert [e.fits_today for e in result] == [True, True, False]


def test_partially_booked_day_queues_through_the_one_free_bay():
    busy = [Busy(at(13), at(17))]
    result = run(now=at(13), queue=jobs(30, 30, 30), busy=busy, capacity=2)
    assert starts(result) == [at(13), at(13, 30), at(14)]


def test_booking_that_starts_mid_service_is_detected_not_just_at_the_start():
    # Two bays. Bay 1 busy 10:00-12:00. A booking takes bay 2 at 10:20. At
    # 10:00 a bay *looks* free, but a 30-minute job would still be running
    # at 10:20 when both bays are needed - so it must wait for 11:00.
    busy = [Busy(at(10), at(12)), Busy(at(10, 20), at(11))]
    assert starts(run(now=at(10), queue=jobs(30), busy=busy, capacity=2)) == [at(11)]


def test_staggered_bookings_find_the_first_real_gap():
    busy = [Busy(at(10), at(11)), Busy(at(10, 30), at(11, 30))]
    result = run(now=at(10), queue=jobs(30, 30), busy=busy, capacity=2)
    # Job 1 takes the free bay 10:00-10:30. Job 2: 10:30 has both bookings
    # running; 11:00 frees one.
    assert starts(result) == [at(10), at(11)]


def test_intervals_entirely_in_the_past_are_ignored():
    busy = [Busy(at(9), at(10))]
    assert starts(run(now=at(11), queue=jobs(30), busy=busy)) == [at(11)]


def test_in_progress_service_holds_its_bay_until_expected_end():
    busy = [ongoing(at(10), at(10, 45), now=at(10, 20))]
    assert starts(run(now=at(10, 20), queue=jobs(30), busy=busy)) == [at(10, 45)]


def test_overrunning_service_pushes_the_queue_back_on_refresh():
    # Expected to end 10:45 but it's 11:00 and still going: the estimate
    # assumes it's about to finish, not that the bay is already free.
    now = at(11)
    busy = [ongoing(at(10), at(10, 45), now=now)]
    assert busy[0].end == now + OVERRUN_GRACE
    assert starts(run(now=now, queue=jobs(30), busy=busy)) == [now + OVERRUN_GRACE]


# --- queue order ---------------------------------------------------------


def test_fifo_is_kept_even_when_a_later_shorter_job_could_fill_a_gap():
    # One bay, a booking at 13:40. The first walk-in (60 min) can't fit before
    # it; the second (20 min) could - but nobody is estimated to start ahead
    # of someone in front of them.
    busy = [Busy(at(13, 40), at(14, 40))]
    result = run(now=at(13), queue=jobs(60, 20), busy=busy)
    assert starts(result) == [at(14, 40), at(15, 40)]


def test_start_times_never_go_backwards_along_the_queue():
    busy = [Busy(at(10), at(10, 50)), Busy(at(11), at(12))]
    result = run(now=at(10), queue=jobs(90, 10, 10, 60), busy=busy, capacity=2)
    times = starts(result)
    assert times == sorted(times)


# --- closing time --------------------------------------------------------


def test_job_that_would_finish_after_closing_does_not_fit():
    result = run(now=at(16, 30), queue=jobs(45))
    assert starts(result) == [None]
    assert result[0].fits_today is False


def test_job_finishing_exactly_at_closing_fits():
    assert starts(run(now=at(16, 30), queue=jobs(30))) == [at(16, 30)]


def test_everyone_behind_a_job_that_misses_closing_also_misses_it():
    # The second job (60 min) can't finish by 17:00; the third is short enough
    # that it technically could, but FIFO means it isn't estimated ahead.
    result = run(now=at(15, 45), queue=jobs(30, 60, 10))
    assert starts(result) == [at(15, 45), None, None]
    assert [e.position for e in result] == [1, 2, 3]


def test_closed_day_gives_no_estimates_but_keeps_positions():
    result = run(now=at(10), queue=jobs(30, 30), opens=None, closes=None)
    assert starts(result) == [None, None]
    assert [e.position for e in result] == [1, 2]


def test_zero_capacity_gives_no_estimates():
    assert starts(run(now=at(10), queue=jobs(30), capacity=0)) == [None]


# --- helpers -------------------------------------------------------------


def test_ongoing_keeps_expected_end_when_not_overrunning():
    assert ongoing(at(10), at(11), now=at(10, 30)) == Busy(at(10), at(11))


def test_wait_minutes_rounds_up_and_floors_at_zero():
    now = at(10)
    assert wait_minutes(now + timedelta(seconds=90), now) == 2
    assert wait_minutes(now + timedelta(minutes=30), now) == 30
    assert wait_minutes(now - timedelta(minutes=5), now) == 0
    assert wait_minutes(None, now) is None
