"""An item added already due within 48 h, behind a full three, pings now - not at its deadline."""
from datetime import timedelta


def test_urgent_queued_add_is_in_the_next_hourly_batch(service, clock):
    for i in range(3):
        service.add(f"busy {i}", area="School", due_at=clock.now() + timedelta(days=9))
    quiz = service.add("quiz closes tonight", area="School", due_at=clock.now() + timedelta(hours=14))
    later = service.add("due next week", area="School", due_at=clock.now() + timedelta(days=6))
    assert quiz.status == "queued" and later.status == "queued"
    assert quiz.next_nag_at == clock.now()                   # in the hourly message from now
    assert later.next_nag_at == later.due_at                  # not urgent: waits for its date
    assert quiz.id in {t.id for t in service.due_batch(clock.now())}
    assert later.id not in {t.id for t in service.due_batch(clock.now())}
