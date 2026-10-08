"""Mail provenance shared by transports and passive readers."""
def message_time(value):
    """RFC mail timestamp with an explicit timezone, or None."""
    from email.utils import parsedate_to_datetime
    try:
        result = parsedate_to_datetime(value)
        return result.isoformat() if result.tzinfo is not None else None
    except (ValueError, TypeError, IndexError):
        return None

