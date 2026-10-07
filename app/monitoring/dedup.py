from datetime import UTC


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def update_slot(record, data, now, cooldown):
    if record.state == "DISAPPEARED":
        elapsed = (now - aware(record.disappeared_at)).total_seconds()
        if elapsed >= cooldown:
            record.generation += 1
            record.state = "AVAILABLE_AGAIN"
        else:
            record.state = "AVAILABLE"
    if record.data.get("count") != data.get("count"):
        record.generation += 1
        record.state = "AVAILABLE_AGAIN"
    record.data = data
    record.last_seen = now
    record.disappeared_at = None


def matches(sub, slot):
    return sub.enabled and not sub.deleted and sub.date_from <= slot.day <= sub.date_to
